// Native capture transport. Build with build_macos_capture.sh; stdout is binary only.
// Apple API references:
// https://developer.apple.com/documentation/coreaudio/catapdescription
// https://developer.apple.com/documentation/coreaudio/audiodeviceioblock
// https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps
import Foundation
import CoreAudio
import AudioToolbox
import AVFoundation
import Darwin

private let systemObject = AudioObjectID(kAudioObjectSystemObject)
private let maxHeader = 64 * 1024
private let maxPayload = 4 * 1024 * 1024

private struct CaptureError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

private func checked(_ status: OSStatus, _ operation: String) throws {
    guard status == noErr else {
        throw CaptureError("\(operation) failed (CoreAudio \(status)). Check the selected device and enable Snipvoice in System Settings > Privacy & Security > Microphone / Screen & System Audio Recording, then start a new recording.")
    }
}

private func address(_ selector: AudioObjectPropertySelector,
                     _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

private func scalar<T>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                       _ initial: T, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) throws -> T {
    var property = address(selector, scope)
    var result = initial
    var size = UInt32(MemoryLayout<T>.size)
    try checked(AudioObjectGetPropertyData(object, &property, 0, nil, &size, &result), "Read audio device property")
    return result
}

private func stringProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) throws -> String {
    let result = try scalar(object, selector, "" as CFString)
    return result as String
}

private func objectList(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) throws -> [AudioObjectID] {
    var property = address(selector, scope)
    var size: UInt32 = 0
    try checked(AudioObjectGetPropertyDataSize(object, &property, 0, nil, &size), "Enumerate audio devices")
    guard size <= 1024 * 1024, size % UInt32(MemoryLayout<AudioObjectID>.size) == 0 else {
        throw CaptureError("CoreAudio returned an invalid device list. Restart the audio device and retry.")
    }
    if size == 0 { return [] }
    var result = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    try result.withUnsafeMutableBytes { buffer in
        try checked(AudioObjectGetPropertyData(object, &property, 0, nil, &size, buffer.baseAddress!), "Enumerate audio devices")
    }
    return result
}

private struct Endpoint {
    let object: AudioObjectID
    let uid: String
    let name: String
    let kind: String
    let isDefault: Bool
}

private func endpoints() throws -> [Endpoint] {
    let inputDefault = try scalar(systemObject, kAudioHardwarePropertyDefaultInputDevice, AudioObjectID(0))
    let outputDefault = try scalar(systemObject, kAudioHardwarePropertyDefaultOutputDevice, AudioObjectID(0))
    var result: [Endpoint] = []
    for device in try objectList(systemObject, kAudioHardwarePropertyDevices) {
        // Private tap aggregates must never be offered as user endpoints.
        if uidIsCaptureAggregate(device) { continue }
        guard let uid = try? stringProperty(device, kAudioDevicePropertyDeviceUID),
              let name = try? stringProperty(device, kAudioObjectPropertyName) else { continue }
        for (kind, scope, defaultDevice) in [
            ("microphone", kAudioDevicePropertyScopeInput, inputDefault),
            ("system", kAudioDevicePropertyScopeOutput, outputDefault)
        ] {
            if let streams = try? objectList(device, kAudioDevicePropertyStreams, scope), !streams.isEmpty {
                result.append(Endpoint(object: device, uid: uid, name: name, kind: kind, isDefault: device == defaultDevice))
            }
        }
    }
    return result
}

private func uidIsCaptureAggregate(_ device: AudioObjectID) -> Bool {
    (try? stringProperty(device, kAudioDevicePropertyDeviceUID))?.hasPrefix("snipvoice-capture-") == true
}

private struct Selection {
    let value: String
    var followsDefault: Bool { value == "default:multimedia" || value == "default:communications" }
    func resolve(_ kind: String, _ devices: [Endpoint]) throws -> Endpoint {
        // CoreAudio has no communications-role default; both roles use the OS default.
        guard let endpoint = devices.first(where: { $0.kind == kind && (followsDefault ? $0.isDefault : $0.uid == value) }) else {
            throw CaptureError("The selected \(kind) device is unavailable. Connect it or choose an available device and start a new recording.")
        }
        return endpoint
    }
}

private final class Transport {
    private let lock = NSLock()
    init() {
        signal(SIGPIPE, SIG_IGN)
        let flags = fcntl(STDOUT_FILENO, F_GETFL)
        if flags >= 0 { _ = fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK) }
    }
    func emit(_ header: [String: Any], _ payload: Data = Data()) throws {
        let json = try JSONSerialization.data(withJSONObject: header, options: [.sortedKeys])
        guard json.count <= maxHeader, payload.count <= maxPayload else { throw CaptureError("Capture event exceeds the transport limit.") }
        var headerLength = UInt32(json.count).littleEndian
        var payloadLength = UInt32(payload.count).littleEndian
        var frame = Data(bytes: &headerLength, count: 4)
        frame.append(json)
        frame.append(Data(bytes: &payloadLength, count: 4))
        frame.append(payload)
        lock.lock()
        defer { lock.unlock() }
        // ceiling: a stalled parent pipe gets two seconds; stop instead of unbounded shutdown.
        let deadline = ProcessInfo.processInfo.systemUptime + 2
        try frame.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = Darwin.write(STDOUT_FILENO, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count > 0 { offset += count; continue }
                if count < 0 && errno == EINTR { continue }
                if count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && ProcessInfo.processInfo.systemUptime < deadline {
                    var descriptor = pollfd(fd: STDOUT_FILENO, events: Int16(POLLOUT), revents: 0)
                    _ = poll(&descriptor, 1, 20)
                    continue
                }
                throw CaptureError("The recording process stopped reading audio. Stop and restart Snipvoice; recover the saved recording from the meeting workspace.")
            }
        }
    }
}

private struct RawBlock {
    let bytes: Data
    let sizes: [Int]
    let channels: [Int]
    let hostTime: UInt64
}

private final class Slot {
    let storage = UnsafeMutableRawPointer.allocate(byteCount: Ring.slotBytes, alignment: 64)
    var sizes = [Int](repeating: 0, count: 8)
    var channels = [Int](repeating: 0, count: 8)
    var buffers = 0
    var bytes = 0
    var frames = 0
    var hostTime: UInt64 = 0
    deinit { storage.deallocate() }
}

private final class Ring {
    // ceiling: 16 MiB / two seconds per source and 1 MiB per callback; larger blocks require a reviewed queue budget.
    static let slotBytes = 1024 * 1024
    private let slots = (0..<16).map { _ in Slot() }
    private var mutex = pthread_mutex_t()
    private var readIndex = 0
    private var writeIndex = 0
    private var count = 0
    private var queuedFrames = 0
    private var busy = false
    private let sampleBytes: Int
    private let maxFrames: Int
    private let dropped = UnsafeMutablePointer<Int32>.allocate(capacity: 1)
    private let invalid = UnsafeMutablePointer<Int32>.allocate(capacity: 1)
    init(_ format: AudioStreamBasicDescription) {
        let planar = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
        sampleBytes = Int(format.mBytesPerFrame) / (planar ? 1 : Int(format.mChannelsPerFrame))
        maxFrames = Int(format.mSampleRate * 2)
        pthread_mutex_init(&mutex, nil)
        dropped.initialize(to: 0)
        invalid.initialize(to: 0)
    }
    deinit {
        pthread_mutex_destroy(&mutex)
        dropped.deinitialize(count: 1); dropped.deallocate()
        invalid.deinitialize(count: 1); invalid.deallocate()
    }
    func push(_ input: UnsafePointer<AudioBufferList>, _ time: UnsafePointer<AudioTimeStamp>) {
        // Callback work is bounded metadata validation and memcpy. Never wait for the consumer.
        guard pthread_mutex_trylock(&mutex) == 0 else { OSAtomicIncrement32Barrier(dropped); return }
        defer { pthread_mutex_unlock(&mutex) }
        guard count < slots.count else { OSAtomicIncrement32Barrier(dropped); return }
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard !buffers.isEmpty, buffers.count <= 8,
              time.pointee.mFlags.contains(.hostTimeValid) else {
            OSAtomicIncrement32Barrier(invalid); return
        }
        let slot = slots[writeIndex]
        let frameBytes = sampleBytes * Int(buffers[0].mNumberChannels)
        guard frameBytes > 0 else { OSAtomicIncrement32Barrier(invalid); return }
        let frames = Int(buffers[0].mDataByteSize) / frameBytes
        guard frames > 0 && frames <= 32768 else { OSAtomicIncrement32Barrier(invalid); return }
        guard queuedFrames + frames <= maxFrames else { OSAtomicIncrement32Barrier(dropped); return }
        var offset = 0
        for (index, buffer) in buffers.enumerated() {
            let size = Int(buffer.mDataByteSize)
            guard size > 0, offset + size <= Ring.slotBytes,
                  buffer.mNumberChannels > 0, buffer.mNumberChannels <= 8,
                  let data = buffer.mData else { OSAtomicIncrement32Barrier(invalid); return }
            memcpy(slot.storage.advanced(by: offset), data, size)
            slot.sizes[index] = size
            slot.channels[index] = Int(buffer.mNumberChannels)
            offset += size
        }
        slot.buffers = buffers.count
        slot.bytes = offset
        slot.frames = frames
        slot.hostTime = time.pointee.mHostTime
        writeIndex = (writeIndex + 1) % slots.count
        count += 1
        queuedFrames += frames
    }
    func pop() -> RawBlock? {
        pthread_mutex_lock(&mutex)
        guard count > 0 && !busy else { pthread_mutex_unlock(&mutex); return nil }
        let slot = slots[readIndex]
        busy = true
        pthread_mutex_unlock(&mutex)
        // Keep the slot counted while copying outside the lock, so the producer cannot overwrite it.
        let block = RawBlock(bytes: Data(bytes: slot.storage, count: slot.bytes),
                             sizes: Array(slot.sizes.prefix(slot.buffers)),
                             channels: Array(slot.channels.prefix(slot.buffers)), hostTime: slot.hostTime)
        return block
    }
    func complete() {
        pthread_mutex_lock(&mutex)
        queuedFrames -= slots[readIndex].frames
        readIndex = (readIndex + 1) % slots.count
        count -= 1
        busy = false
        pthread_mutex_unlock(&mutex)
    }
    func isDrained() -> Bool {
        pthread_mutex_lock(&mutex)
        defer { pthread_mutex_unlock(&mutex) }
        return count == 0 && !busy
    }
    func losses() -> (Int32, Int32) {
        let overflow = OSAtomicAdd32Barrier(0, dropped)
        let badInput = OSAtomicAdd32Barrier(0, invalid)
        return (overflow, badInput)
    }
}

private func validateFormat(_ format: AudioStreamBasicDescription) throws {
    let bits = Int(format.mBitsPerChannel)
    let channels = Int(format.mChannelsPerFrame)
    let floating = format.mFormatFlags & kAudioFormatFlagIsFloat != 0
    let planar = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
    let sampleBytes = Int(format.mBytesPerFrame) / (planar ? 1 : max(channels, 1))
    guard format.mFormatID == kAudioFormatLinearPCM, channels >= 1, channels <= 8,
          format.mSampleRate.isFinite, format.mSampleRate >= 8000, format.mSampleRate <= 192000,
          format.mSampleRate.rounded() == format.mSampleRate,
          [8, 16, 24, 32, 64].contains(bits), sampleBytes >= bits / 8, sampleBytes <= 8,
          (floating ? (bits == 32 || bits == 64) : bits <= 32),
          (!floating || sampleBytes == bits / 8),
          format.mBytesPerFrame % UInt32(planar ? 1 : channels) == 0 else {
        throw CaptureError("The selected audio device uses an unsupported native PCM format. Choose a device with 1–8 PCM channels at an integer rate between 8 and 192 kHz.")
    }
}

private func interleave(_ block: RawBlock, _ format: AudioStreamBasicDescription) throws -> (Data, Int) {
    let channelCount = Int(format.mChannelsPerFrame)
    let planar = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
    let sampleBytes = Int(format.mBytesPerFrame) / (planar ? 1 : channelCount)
    let bits = Int(format.mBitsPerChannel)
    let bigEndian = format.mFormatFlags & kAudioFormatFlagIsBigEndian != 0
    let floating = format.mFormatFlags & kAudioFormatFlagIsFloat != 0
    let signed = format.mFormatFlags & kAudioFormatFlagIsSignedInteger != 0
    let highAligned = format.mFormatFlags & kAudioFormatFlagIsAlignedHigh != 0
    guard block.channels.reduce(0, +) == channelCount else {
        throw CaptureError("The audio device changed channel layout. Restart the recording with its current format.")
    }
    let frames = block.sizes[0] / (sampleBytes * block.channels[0])
    guard frames > 0, frames <= 32768, frames * channelCount * 4 <= maxPayload,
          zip(block.sizes, block.channels).allSatisfy({ size, channels in
              size % (sampleBytes * channels) == 0 && size / (sampleBytes * channels) == frames
          }) else { throw CaptureError("The audio device returned an invalid or oversized PCM block. Select a smaller device buffer and restart recording.") }
    var output = Data(count: frames * channelCount * 4)
    block.bytes.withUnsafeBytes { source in
        output.withUnsafeMutableBytes { destination in
            let input = source.bindMemory(to: UInt8.self)
            let out = destination.bindMemory(to: UInt8.self)
            var base = 0
            var channelBase = 0
            for buffer in block.sizes.indices {
                let channels = block.channels[buffer]
                for frame in 0..<frames {
                    for channel in 0..<channels {
                        let offset = base + (frame * channels + channel) * sampleBytes
                        var word: UInt64 = 0
                        for byte in 0..<sampleBytes {
                            let shift = (bigEndian ? sampleBytes - byte - 1 : byte) * 8
                            word |= UInt64(input[offset + byte]) << shift
                        }
                        let value: Float
                        if floating {
                            value = bits == 32 ? Float(bitPattern: UInt32(truncatingIfNeeded: word)) : Float(Double(bitPattern: word))
                        } else {
                            if highAligned { word >>= sampleBytes * 8 - bits }
                            let mask = (UInt64(1) << bits) - 1
                            word &= mask
                            let midpoint = Double(UInt64(1) << (bits - 1))
                            if signed && word & (UInt64(1) << (bits - 1)) != 0 {
                                value = Float((Double(word) - 2 * midpoint) / midpoint)
                            } else { value = Float((Double(word) - (signed ? 0 : midpoint)) / midpoint) }
                        }
                        let encoded = (value.isFinite ? value : 0).bitPattern
                        let outputOffset = (frame * channelCount + channelBase + channel) * 4
                        for byte in 0..<4 { out[outputOffset + byte] = UInt8(truncatingIfNeeded: encoded >> (byte * 8)) }
                    }
                }
                base += block.sizes[buffer]
                channelBase += channels
            }
        }
    }
    return (output, frames)
}

private final class Sequence {
    private let lock = NSLock()
    private var value = 0
    func next() -> Int { lock.lock(); defer { lock.unlock() }; let result = value; value += 1; return result }
}

@available(macOS 14.4, *)
private final class Source {
    let kind: String
    let endpoint: Endpoint
    let format: AudioStreamBasicDescription
    private let transport: Transport
    private let generation: Int
    private let origin: UInt64
    private let sequence: Sequence
    private let ring: Ring
    private var device: AudioObjectID
    private var tap: AudioObjectID = 0
    private var aggregate: AudioObjectID = 0
    private var ioProc: AudioDeviceIOProcID?
    private let state = NSLock()
    private var quitting = false
    private var failure: String?
    private let finished = DispatchSemaphore(value: 0)
    private var started = false
    private var closed = false
    var error: String? { state.lock(); defer { state.unlock() }; return failure }

    init(_ kind: String, _ endpoint: Endpoint, _ transport: Transport, _ generation: Int, _ origin: UInt64, _ sequence: Sequence) throws {
        self.kind = kind; self.endpoint = endpoint; self.transport = transport
        self.generation = generation; self.origin = origin; self.sequence = sequence; device = endpoint.object
        var nativeFormat = AudioStreamBasicDescription()
        var createdTap: AudioObjectID = 0
        var createdAggregate: AudioObjectID = 0
        do {
            if kind == "system" {
                let streams = try objectList(endpoint.object, kAudioDevicePropertyStreams, kAudioDevicePropertyScopeOutput)
                // ceiling: one output stream per endpoint. Reject multi-stream hardware instead of silently capturing a subset.
                guard streams.count == 1 else { throw CaptureError("This output device has multiple native streams. Choose a single-stream output device for system recording.") }
                let description = CATapDescription(excludingProcesses: [], deviceUID: endpoint.uid, stream: 0)
                description.name = "Snipvoice private output capture"
                description.isPrivate = true
                description.muteBehavior = .unmuted
                try checked(AudioHardwareCreateProcessTap(description, &createdTap), "Create system-audio tap")
                nativeFormat = try scalar(createdTap, kAudioTapPropertyFormat, nativeFormat)
                let tapUID = try stringProperty(createdTap, kAudioTapPropertyUID)
                let dictionary: [String: Any] = [
                    kAudioAggregateDeviceNameKey: "Snipvoice private capture",
                    kAudioAggregateDeviceUIDKey: "snipvoice-capture-" + UUID().uuidString,
                    kAudioAggregateDeviceIsPrivateKey: true,
                    kAudioAggregateDeviceIsStackedKey: false,
                    kAudioAggregateDeviceTapAutoStartKey: true,
                    kAudioAggregateDeviceTapListKey: [[
                        kAudioSubTapUIDKey: tapUID,
                        kAudioSubTapDriftCompensationKey: true
                    ]]
                ]
                // A tap-only aggregate has one clock; no playback subdevice is opened or rerouted.
                try checked(AudioHardwareCreateAggregateDevice(dictionary as CFDictionary, &createdAggregate), "Create private tap aggregate")
            } else {
                nativeFormat = try scalar(endpoint.object, kAudioDevicePropertyStreamFormat, nativeFormat, kAudioDevicePropertyScopeInput)
            }
            try validateFormat(nativeFormat)
        } catch {
            if createdAggregate != 0 { AudioHardwareDestroyAggregateDevice(createdAggregate) }
            if createdTap != 0 { AudioHardwareDestroyProcessTap(createdTap) }
            throw error
        }
        format = nativeFormat; tap = createdTap; aggregate = createdAggregate
        ring = Ring(nativeFormat)
        if aggregate != 0 { device = aggregate }
        do {
            let queue = ring
            try checked(AudioDeviceCreateIOProcIDWithBlock(&ioProc, device, nil) { _, input, inputTime, _, _ in
                queue.push(input, inputTime)
            }, "Create audio capture callback")
        } catch {
            if aggregate != 0 { AudioHardwareDestroyAggregateDevice(aggregate) }
            if tap != 0 { AudioHardwareDestroyProcessTap(tap) }
            throw error
        }
        Thread.detachNewThread { [self] in consume() }
    }

    func start() throws {
        try checked(AudioDeviceStart(device, ioProc), "Start \(kind) capture")
        started = true
    }
    func pause() throws {
        if started {
            try checked(AudioDeviceStop(device, ioProc), "Pause \(kind) capture")
            started = false
        }
        flush()
    }
    func flush() {
        let deadline = ProcessInfo.processInfo.systemUptime + 2.5
        while !ring.isDrained() && ProcessInfo.processInfo.systemUptime < deadline { usleep(2000) }
    }
    func close() {
        guard !closed else { return }
        closed = true
        // CoreAudio teardown is synchronous; a wedged driver must not hold the helper forever.
        let watchdog = DispatchWorkItem {
            diagnostic("Native audio cleanup exceeded eight seconds. Restart Snipvoice and reconnect the audio device; recover the saved recording.")
            _exit(1)
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 8, execute: watchdog)
        defer { watchdog.cancel() }
        do { try pause() } catch { reportCleanup(error) }
        if let ioProc {
            do { try checked(AudioDeviceDestroyIOProcID(device, ioProc), "Destroy \(kind) capture callback") }
            catch { reportCleanup(error) }
            self.ioProc = nil
        }
        state.lock(); quitting = true; state.unlock()
        if finished.wait(timeout: .now() + 3) == .timedOut { reportCleanup(CaptureError("The audio writer did not stop within three seconds. Restart Snipvoice and recover the saved recording.")) }
        if aggregate != 0 {
            do { try checked(AudioHardwareDestroyAggregateDevice(aggregate), "Destroy private capture aggregate") }
            catch { reportCleanup(error) }
            aggregate = 0
        }
        if tap != 0 {
            do { try checked(AudioHardwareDestroyProcessTap(tap), "Destroy system-audio tap") }
            catch { reportCleanup(error) }
            tap = 0
        }
    }
    private func reportCleanup(_ error: Error) {
        state.lock(); failure = String(describing: error); state.unlock()
        do { try transport.emit(["type": "source_error", "generation": generation, "track": kind,
                                  "timestamp": elapsed(), "message": String(describing: error)]) }
        catch { diagnostic(String(describing: error)) }
    }
    private func consume() {
        defer { finished.signal() }
        var previousLosses: (Int32, Int32) = (0, 0)
        var previousEnd: Double?
        while true {
            state.lock(); let quit = quitting; state.unlock()
            do {
                let losses = ring.losses()
                if losses.0 != previousLosses.0 || losses.1 != previousLosses.1 {
                    let reason = losses.1 != previousLosses.1 ? "invalid_native_buffer_or_timestamp" : "capture_queue_overflow"
                    try transport.emit(["type": "gap", "generation": generation, "track": kind,
                                        "timestamp": elapsed(), "reason": reason])
                    previousLosses = losses
                }
                if let block = ring.pop() {
                    defer { ring.complete() }
                    let nativeTime = AudioConvertHostTimeToNanos(block.hostTime)
                    guard nativeTime >= origin else { throw CaptureError("The audio device returned a capture timestamp before the session origin. Restart recording.") }
                    let timestamp = Double(nativeTime - origin) / 1_000_000_000
                    let (payload, frames) = try interleave(block, format)
                    if let end = previousEnd, abs(timestamp - end) > max(0.02, 2 * Double(frames) / format.mSampleRate) {
                        try transport.emit(["type": "gap", "generation": generation, "track": kind,
                                            "timestamp": end, "reason": "native_clock_discontinuity"])
                    }
                    try transport.emit(["type": "audio", "generation": generation, "track": kind,
                                        "rate": Int(format.mSampleRate), "channels": Int(format.mChannelsPerFrame),
                                        "frames": frames, "timestamp": timestamp, "sequence": sequence.next()], payload)
                    previousEnd = timestamp + Double(frames) / format.mSampleRate
                } else if quit { return }
                else { usleep(2000) }
            } catch {
                state.lock(); failure = String(describing: error); state.unlock()
                return
            }
        }
    }
    private func elapsed() -> Double {
        Double(AudioConvertHostTimeToNanos(AudioGetCurrentHostTime()) - origin) / 1_000_000_000
    }
}

private final class Commands {
    private let lock = NSLock()
    private var pending: [String] = []
    func add(_ command: String) {
        lock.lock(); defer { lock.unlock() }
        // ceiling: 64 pending controls; excess controls stop capture instead of growing memory.
        if pending.count >= 64 { pending = ["stop"] } else { pending.append(command) }
    }
    func take() -> [String] { lock.lock(); defer { lock.unlock() }; let result = pending; pending.removeAll(); return result }
    func read() {
        Thread.detachNewThread { [self] in
            var line = Data()
            var byte: UInt8 = 0
            while true {
                let count = Darwin.read(STDIN_FILENO, &byte, 1)
                if count < 0 && errno == EINTR { continue }
                guard count == 1 else { add("stop"); return }
                if byte == 10 {
                    guard let json = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                          let command = json["command"] as? String, ["pause", "resume", "stop"].contains(command) else {
                        add("invalid"); line.removeAll(keepingCapacity: true); continue
                    }
                    add(command); line.removeAll(keepingCapacity: true)
                } else {
                    guard line.count < 4096 else { add("invalid"); add("stop"); return }
                    line.append(byte)
                }
            }
        }
    }
}

@available(macOS 14.4, *)
private func capture(_ transport: Transport, _ options: [String: String], _ generation: Int) throws {
    let origin = AudioConvertHostTimeToNanos(AudioGetCurrentHostTime())
    func elapsed() -> Double { Double(AudioConvertHostTimeToNanos(AudioGetCurrentHostTime()) - origin) / 1_000_000_000 }
    func event(_ type: String, _ kind: String, _ values: [String: Any]) throws {
        var header: [String: Any] = ["type": type, "generation": generation, "track": kind, "timestamp": elapsed()]
        values.forEach { header[$0.key] = $0.value }
        try transport.emit(header)
    }
    let kinds = options["--sources"] == "both" ? ["microphone", "system"] : [options["--sources"]!]
    var sources: [String: Source] = [:]
    var selections: [String: Selection] = [:]
    var lastErrors: [String: String] = [:]
    let sequences = Dictionary(uniqueKeysWithValues: kinds.map { ($0, Sequence()) })
    var paused = false
    var stopped = false
    let commands = Commands()
    let listenerQueue = DispatchQueue(label: "snipvoice.capture.device-notices")
    let shutdownSignals: [DispatchSourceSignal] = [SIGINT, SIGTERM].map { number in
        signal(number, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: number, queue: listenerQueue)
        source.setEventHandler { commands.add("stop") }
        source.resume()
        return source
    }
    let listener: AudioObjectPropertyListenerBlock = { _, _ in commands.add("refresh") }
    let selectors = [kAudioHardwarePropertyDevices, kAudioHardwarePropertyDefaultInputDevice, kAudioHardwarePropertyDefaultOutputDevice]
    var installed: [AudioObjectPropertyAddress] = []
    for selector in selectors {
        var property = address(selector)
        if AudioObjectAddPropertyListenerBlock(systemObject, &property, listenerQueue, listener) == noErr { installed.append(property) }
    }
    defer {
        shutdownSignals.forEach { $0.cancel() }
        for var property in installed { AudioObjectRemovePropertyListenerBlock(systemObject, &property, listenerQueue, listener) }
        sources.values.forEach { $0.close() }
    }
    for kind in kinds { selections[kind] = Selection(value: options["--\(kind)"] ?? "default:multimedia") }
    try transport.emit(["type": "ready", "version": 1, "generation": generation])
    commands.read()
    if kinds.contains("microphone") && AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
        // Ask only after explicit capture admission. Enumeration and self-test never request permission.
        AVCaptureDevice.requestAccess(for: .audio) { _ in commands.add("refresh") }
    }
    func awaitingMicrophone() -> Bool {
        kinds.contains("microphone") && AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined && elapsed() < 60
    }

    func refresh() throws {
        let devices = try endpoints()
        for kind in kinds {
            do {
                if kind == "microphone" {
                    switch AVCaptureDevice.authorizationStatus(for: .audio) {
                    case .authorized: break
                    case .notDetermined:
                        throw CaptureError("Approve the macOS microphone permission dialog to record your microphone. Recording waits up to 60 seconds; you can stop at any time.")
                    case .denied, .restricted:
                        throw CaptureError("Microphone access is denied. Enable Snipvoice in System Settings > Privacy & Security > Microphone, then start a new recording.")
                    @unknown default: throw CaptureError("macOS returned an unknown microphone permission status. Restart Snipvoice and retry.")
                    }
                }
                let endpoint = try selections[kind]!.resolve(kind, devices)
                let alive = try scalar(endpoint.object, kAudioDevicePropertyDeviceIsAlive, UInt32(0))
                guard alive != 0 else { throw CaptureError("The selected \(kind) device disconnected. Reconnect it or choose another device for the next recording.") }
                if let current = sources[kind] {
                    if let error = current.error { throw CaptureError(error) }
                    let scope = kind == "microphone" ? kAudioDevicePropertyScopeInput : kAudioDevicePropertyScopeOutput
                    let native = try scalar(endpoint.object, kAudioDevicePropertyStreamFormat, AudioStreamBasicDescription(), scope)
                    if current.endpoint.uid == endpoint.uid && current.endpoint.object == endpoint.object && native.mSampleRate == current.format.mSampleRate && native.mChannelsPerFrame == current.format.mChannelsPerFrame && native.mFormatID == current.format.mFormatID && native.mFormatFlags == current.format.mFormatFlags && native.mBitsPerChannel == current.format.mBitsPerChannel && native.mBytesPerFrame == current.format.mBytesPerFrame { continue }
                    current.close(); sources.removeValue(forKey: kind)
                    try event("gap", kind, ["reason": "device_or_format_changed"])
                }
                let source = try Source(kind, endpoint, transport, generation, origin, sequences[kind]!)
                sources[kind] = source
                try event("source_changed", kind, ["endpoint_id": endpoint.uid])
                if !paused { try source.start() }
                lastErrors.removeValue(forKey: kind)
            } catch {
                sources.removeValue(forKey: kind)?.close()
                let message = String(describing: error)
                if lastErrors[kind] != message {
                    try event("source_error", kind, ["message": message])
                    try event("gap", kind, ["reason": "source_unavailable"])
                    lastErrors[kind] = message
                }
            }
        }
        if sources.isEmpty && !awaitingMicrophone() { stopped = true }
    }
    try refresh()
    var lastRefresh = ProcessInfo.processInfo.systemUptime
    while !stopped {
        for command in commands.take() {
            switch command {
            case "stop": stopped = true
            case "pause" where !paused:
                paused = true
                for kind in kinds {
                    do { try sources[kind]?.pause() }
                    catch { sources.removeValue(forKey: kind)?.close(); try event("source_error", kind, ["message": String(describing: error)]) }
                }
                for kind in kinds { try event("gap", kind, ["reason": "paused"]) }
                if sources.isEmpty && !awaitingMicrophone() { stopped = true }
            case "resume" where paused:
                paused = false
                for kind in kinds {
                    try event("gap", kind, ["reason": "resumed"])
                    do { try sources[kind]?.start() }
                    catch { sources.removeValue(forKey: kind)?.close(); try event("source_error", kind, ["message": String(describing: error)]) }
                }
                if sources.isEmpty && !awaitingMicrophone() { stopped = true }
            case "refresh": try refresh(); lastRefresh = ProcessInfo.processInfo.systemUptime
            case "invalid": throw CaptureError("Invalid capture control message. Send newline-delimited JSON with command pause, resume, or stop.")
            default: break
            }
            if stopped { break }
        }
        // Poll alive/format as well as default listeners: endpoint unplug and in-place format changes must be visible.
        if !stopped && ProcessInfo.processInfo.systemUptime - lastRefresh >= 0.25 {
            try refresh(); lastRefresh = ProcessInfo.processInfo.systemUptime
        }
        if !stopped { usleep(10000) }
    }
    for source in sources.values { source.close() }
    sources.removeAll()
    try transport.emit(["type": "stopped", "generation": generation])
}

private func run() throws {
    let arguments = Array(CommandLine.arguments.dropFirst())
    let transport = Transport()
    if arguments == ["--self-test"] {
        // This path must never enumerate/open devices or trigger recording permission.
        try transport.emit(["type": "ready", "version": 1, "generation": 0])
        try transport.emit(["type": "stopped", "generation": 0])
        return
    }
    if arguments == ["--list"] {
        let devices = try endpoints().map { endpoint -> [String: Any] in
            ["id": endpoint.uid, "name": endpoint.name, "kind": endpoint.kind,
             "default": endpoint.isDefault, "communications_default": endpoint.isDefault]
        }
        try transport.emit(["type": "devices", "version": 1, "devices": devices])
        return
    }
    guard arguments.first == "--capture", arguments.count % 2 == 1 else { throw CaptureError("Usage: snipvoice-capture --self-test | --list | --capture --sources both|microphone|system --generation INTEGER [--microphone UID|default:multimedia|default:communications] [--system UID|default:multimedia|default:communications]") }
    var options: [String: String] = [:]
    for index in stride(from: 1, to: arguments.count, by: 2) {
        let key = arguments[index]
        guard ["--sources", "--generation", "--microphone", "--system"].contains(key), options[key] == nil,
              !arguments[index + 1].isEmpty, arguments[index + 1].utf8.count <= 4096 else { throw CaptureError("Invalid or duplicate capture option.") }
        options[key] = arguments[index + 1]
    }
    guard let source = options["--sources"], ["both", "microphone", "system"].contains(source),
          let value = options["--generation"], let generation = Int(value), generation >= 0 else { throw CaptureError("Capture requires --sources both|microphone|system and a nonnegative --generation INTEGER.") }
    if #available(macOS 14.4, *) { try capture(transport, options, generation) }
    else { throw CaptureError("Meeting capture requires macOS 14.4 or later. Update macOS or record on a supported computer.") }
}

private func diagnostic(_ detail: String) {
    let message = "Snipvoice capture: \(detail)\n"
    message.withCString { pointer in _ = Darwin.write(STDERR_FILENO, pointer, strlen(pointer)) }
}

do { try run() }
catch {
    diagnostic(String(describing: error))
    exit(1)
}
