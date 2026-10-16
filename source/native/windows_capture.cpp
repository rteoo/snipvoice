// Owned WASAPI helper. stdout is exclusively the version-1 framed protocol.
// https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording
// GetBuffer's QPC timestamp is already in 100 ns units, not raw QPC ticks.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <endpointvolume.h>
#include <functiondiscoverykeys_devpkey.h>
#include <ks.h>
#include <ksmedia.h>
#include <wrl/client.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <iomanip>
#include <iostream>
#include <limits>
#include <locale>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include "capture_health.h"

using Microsoft::WRL::ComPtr;
using Clock = std::chrono::steady_clock;
constexpr size_t kMaxHeader = 64 * 1024;
constexpr size_t kMaxPayload = 4 * 1024 * 1024;
// ceiling: ten seconds per track and 16 MiB total queued PCM; upgrade only with
// measured disk/pipe latency requirements. Overflow is a visible fatal gap.
constexpr size_t kQueueBytes = 16 * 1024 * 1024;
constexpr double kQueueSeconds = 10.0;
constexpr size_t kQueueEvents = 1024;

struct ComScope {
    HRESULT result = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    ComScope() { if (FAILED(result)) throw std::runtime_error("COM initialization failed"); }
    ~ComScope() { CoUninitialize(); }
};
struct Handle {
    HANDLE value = nullptr;
    explicit Handle(HANDLE h = nullptr) : value(h) {}
    ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
};
struct Wave {
    WAVEFORMATEX* value = nullptr;
    ~Wave() { CoTaskMemFree(value); }
};
std::string utf8(const std::wstring& input) {
    if (input.empty()) return {};
    int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, input.data(),
        static_cast<int>(input.size()), nullptr, 0, nullptr, nullptr);
    if (!count) throw std::runtime_error("Device name is not valid Unicode");
    std::string output(count, '\0');
    WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, input.data(),
        static_cast<int>(input.size()), &output[0], count, nullptr, nullptr);
    return output;
}
std::string quote(const std::string& input) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : input) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 32) out << "\\u00" << std::hex << std::setw(2)
                                << std::setfill('0') << static_cast<int>(c) << std::dec;
        else out << c;
    }
    out << '"';
    return out.str();
}
std::string number(double n) {
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out << std::setprecision(15) << n;
    return out.str();
}
bool nextExpectedDevicePosition(UINT64 devicePosition, UINT32 frames, UINT64& next) {
    if (devicePosition > std::numeric_limits<UINT64>::max() - frames) return false;
    next = devicePosition + frames;
    return true;
}

// Keep a packet-contiguous sample timeline anchored to QPC only when the
// endpoint's device position proves that continuity was preserved. QPC can
// step while the audio clock continues; a device-position jump, invalid
// timestamp, pause, or reopen must retain the QPC boundary and stay visible.
struct SampleTimeline {
    bool have = false;
    double anchorTimestamp = 0;
    UINT64 anchorDevicePosition = 0;
    UINT64 expectedDevicePosition = 0;

    void reset() { have = false; anchorTimestamp = 0; anchorDevicePosition = 0; expectedDevicePosition = 0; }

    double timestamp(double qpcTimestamp, UINT64 devicePosition, UINT32 frames,
                     unsigned rate, bool valid) {
        const bool continuous = have && devicePosition == expectedDevicePosition;
        if (!valid || !nextExpectedDevicePosition(devicePosition, frames, expectedDevicePosition)) {
            reset();
            return qpcTimestamp;
        }
        if (!continuous) {
            anchorTimestamp = qpcTimestamp;
            anchorDevicePosition = devicePosition;
        }
        const double selected = anchorTimestamp
            + static_cast<double>(devicePosition - anchorDevicePosition) / rate;
        have = true;
        return selected;
    }
};
std::string nativePacketFields(DWORD flags, UINT32 frames, unsigned rate,
                               UINT64 devicePosition, bool haveExpected,
                               UINT64 expectedDevicePosition, bool positionsValid,
                               double qpcTimestamp = std::numeric_limits<double>::quiet_NaN(),
                               double sampleTimestamp = std::numeric_limits<double>::quiet_NaN()) {
    std::ostringstream out;
    out << ",\"buffer_flags\":" << flags
        << ",\"packet_frames\":" << frames
        << ",\"sample_rate\":" << rate
        << ",\"device_position\":";
    if (positionsValid) out << devicePosition;
    else out << "null";
    out << ",\"expected_device_position\":";
    if (positionsValid && haveExpected) out << expectedDevicePosition;
    else out << "null";
    if (std::isfinite(qpcTimestamp)) out << ",\"qpc_timestamp\":" << number(qpcTimestamp);
    if (std::isfinite(sampleTimestamp)) out << ",\"sample_timestamp\":" << number(sampleTimestamp);
    return out.str();
}
struct AudioFailure : std::runtime_error {
    HRESULT result;
    // The endpoint is absent or inactive: no audio can arrive until the user
    // reconnects it (or, for a default source, until a new default exists).
    bool endpointMissing;
    AudioFailure(HRESULT value, const std::string& message, bool missing = false)
        : std::runtime_error(message), result(value), endpointMissing(missing) {}
};
bool recoverable(HRESULT result) {
    return result == AUDCLNT_E_DEVICE_INVALIDATED
        || result == AUDCLNT_E_RESOURCES_INVALIDATED
        || result == AUDCLNT_E_SERVICE_NOT_RUNNING
        || result == HRESULT_FROM_WIN32(ERROR_NOT_FOUND);
}
void check(HRESULT hr, const char* action) {
    if (SUCCEEDED(hr)) return;
    std::ostringstream message;
    message << action << " failed (HRESULT 0x" << std::hex
            << static_cast<unsigned long>(hr)
            << "). Check the selected endpoint, Windows microphone privacy access, "
               "and the Windows Audio service; refresh device selection and retry.";
    throw AudioFailure(hr, message.str());
}
ComPtr<IMMDeviceEnumerator> enumerator() {
    ComPtr<IMMDeviceEnumerator> out;
    check(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
        IID_PPV_ARGS(out.GetAddressOf())), "Create endpoint enumerator");
    return out;
}
std::wstring deviceId(IMMDevice* device) {
    LPWSTR raw = nullptr;
    check(device->GetId(&raw), "Read endpoint ID");
    std::wstring out(raw);
    CoTaskMemFree(raw);
    return out;
}
std::wstring defaultId(IMMDeviceEnumerator* devices, EDataFlow flow, ERole role) {
    ComPtr<IMMDevice> device;
    HRESULT hr = devices->GetDefaultAudioEndpoint(flow, role, device.GetAddressOf());
    if (hr == E_NOTFOUND) return {};
    check(hr, "Resolve OS default endpoint");
    return deviceId(device.Get());
}
std::string listDevices() {
    auto devices = enumerator();
    std::ostringstream out;
    out << "{\"type\":\"devices\",\"version\":1,\"devices\":[";
    bool first = true;
    for (EDataFlow flow : {eCapture, eRender}) {
        auto multimedia = defaultId(devices.Get(), flow, eMultimedia);
        auto communications = defaultId(devices.Get(), flow, eCommunications);
        ComPtr<IMMDeviceCollection> collection;
        check(devices->EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE,
            collection.GetAddressOf()), "Enumerate active endpoints");
        UINT count = 0;
        check(collection->GetCount(&count), "Count endpoints");
        for (UINT i = 0; i < count; ++i) {
            ComPtr<IMMDevice> device;
            check(collection->Item(i, device.GetAddressOf()), "Read endpoint");
            auto id = deviceId(device.Get());
            ComPtr<IPropertyStore> properties;
            check(device->OpenPropertyStore(STGM_READ, properties.GetAddressOf()), "Read endpoint properties");
            PROPVARIANT name;
            PropVariantInit(&name);
            HRESULT hr = properties->GetValue(PKEY_Device_FriendlyName, &name);
            std::wstring label = (SUCCEEDED(hr) && name.vt == VT_LPWSTR && name.pwszVal)
                ? name.pwszVal : id;
            PropVariantClear(&name);
            if (!first) out << ',';
            first = false;
            out << "{\"id\":" << quote(utf8(id)) << ",\"name\":" << quote(utf8(label))
                << ",\"kind\":\"" << (flow == eCapture ? "microphone" : "system")
                << "\",\"default\":" << (id == multimedia ? "true" : "false")
                << ",\"communications_default\":" << (id == communications ? "true" : "false") << '}';
        }
    }
    out << "]}";
    if (out.str().size() > kMaxHeader) throw std::runtime_error("Device enumeration exceeds protocol limit");
    return out.str();
}

bool writeBytes(HANDLE output, const void* data, size_t length) {
    auto bytes = static_cast<const BYTE*>(data);
    while (length) {
        DWORD written = 0;
        DWORD amount = static_cast<DWORD>(std::min<size_t>(length, 64 * 1024));
        if (!WriteFile(output, bytes, amount, &written, nullptr) || !written) return false;
        bytes += written;
        length -= written;
    }
    return true;
}
bool frame(HANDLE output, const std::string& json, const std::vector<BYTE>& pcm = {}) {
    if (json.empty() || json.size() > kMaxHeader || pcm.size() > kMaxPayload) return false;
    uint32_t header = static_cast<uint32_t>(json.size());
    uint32_t payload = static_cast<uint32_t>(pcm.size());
    return writeBytes(output, &header, 4) && writeBytes(output, json.data(), header)
        && writeBytes(output, &payload, 4) && writeBytes(output, pcm.data(), payload);
}
struct Message {
    std::string header;
    std::vector<BYTE> payload;
    int track = -1;
    double duration = 0;
};
struct Session {
    int64_t generation = 0;
    double origin = 0;
    std::atomic<bool> stop{false}, paused{false};
    std::atomic<unsigned> pauseEpoch{0};
    std::atomic<int> alive{0};
    std::mutex mutex;
    std::condition_variable changed;
    std::deque<Message> queue;
    size_t bytes = 0;
    double durations[2] = {0, 0};
    bool finished = false;
    bool overflow = false;
    std::atomic<bool> writerDone{false}, controlDone{false};
#ifdef SNIPVOICE_CAPTURE_FAULT_TESTS
    // Explicit private test builds only. Official bundles contain no fault
    // arguments or injection behavior.
    std::wstring faultScript;
    bool faultInjected = false;
#endif
    double now() const {
        LARGE_INTEGER counter, frequency;
        QueryPerformanceCounter(&counter);
        QueryPerformanceFrequency(&frequency);
        return static_cast<double>(counter.QuadPart) / frequency.QuadPart - origin;
    }
    std::string prefix(const char* type) const {
        return "{\"type\":" + quote(type) + ",\"generation\":" + std::to_string(generation);
    }
    bool push(Message message) {
        std::lock_guard<std::mutex> lock(mutex);
        if (finished) return false;
        if (message.header.size() > kMaxHeader || message.payload.size() > kMaxPayload) return false;
        if (message.track >= 0 && (bytes + message.payload.size() > kQueueBytes
            || durations[message.track] + message.duration > kQueueSeconds
            || queue.size() >= kQueueEvents)) {
            if (!overflow) {
                overflow = true;
                queue.push_back({prefix("gap") + ",\"track\":"
                    + quote(message.track == 0 ? "microphone" : "system")
                    + ",\"timestamp\":" + number(now())
                    + ",\"reason\":\"transport_overflow: recording stopped; consumer could not keep up\"}"});
            }
            stop = true;
            changed.notify_all();
            return false;
        }
        // ceiling: 1024 queued source events/audio blocks, plus one terminal event.
        bytes += message.payload.size();
        if (message.track >= 0) durations[message.track] += message.duration;
        queue.push_back(std::move(message));
        changed.notify_all();
        return true;
    }
    void event(const char* type, const std::string& fields, int track = -1) {
        push({prefix(type) + fields + '}', {}, track});
    }
    void gap(int track, double timestamp, const char* reason) {
        event("gap", ",\"track\":" + quote(track == 0 ? "microphone" : "system")
            + ",\"timestamp\":" + number(timestamp) + ",\"reason\":" + quote(reason), track);
    }
    void nativeGap(int track, double timestamp, const char* reason,
                   const std::string& packetFields) {
        event("gap", ",\"track\":" + quote(track == 0 ? "microphone" : "system")
            + ",\"timestamp\":" + number(timestamp) + ",\"reason\":" + quote(reason)
            + packetFields, track);
    }
    void error(int track, const std::string& message) {
        event("source_error", ",\"track\":" + quote(track == 0 ? "microphone" : "system")
            + ",\"timestamp\":" + number(now()) + ",\"message\":" + quote(message), track);
    }
};

// Combine clock-contiguous WASAPI packets before sending them to durable storage.
// Two 10 ms sources otherwise require roughly 400 fsync calls per second.
struct AudioBatch {
    Session& session;
    int track;
    unsigned rate, channels, frames = 0;
    uint64_t& sequence;
    double timestamp = 0;
    DWORD packetFlags = 0;
    std::vector<BYTE> payload;
    unsigned maximum() const { return std::max(1u, rate / 10); }
    void flush() {
        if (!frames) return;
        Message block;
        block.track = track;
        block.duration = static_cast<double>(frames) / rate;
        block.payload = std::move(payload);
        block.header = session.prefix("audio") + ",\"track\":"
            + quote(track == 0 ? "microphone" : "system")
            + ",\"rate\":" + std::to_string(rate)
            + ",\"channels\":" + std::to_string(channels)
            + ",\"frames\":" + std::to_string(frames)
            + ",\"timestamp\":" + number(timestamp)
            + ",\"sequence\":" + std::to_string(sequence++);
        if (packetFlags) block.header += ",\"buffer_flags\":" + std::to_string(packetFlags);
        block.header += '}';
        session.push(std::move(block));
        frames = 0;
        packetFlags = 0;
        payload.clear();
    }
    void append(double stamp, unsigned count, const std::vector<BYTE>& pcm, DWORD flags = 0) {
        // ceiling: 100 ms of PCM per source. Real gaps/overlaps, pause, errors
        // and device changes flush; the native sample timeline has already
        // separated continuous device-clock progression from those boundaries.
        if (frames && (frames + count > maximum()
            || std::abs(stamp - (timestamp + static_cast<double>(frames) / rate)) > 0.002))
            flush();
        if (!frames) timestamp = stamp;
        packetFlags |= flags;
        payload.insert(payload.end(), pcm.begin(), pcm.end());
        frames += count;
        if (frames >= maximum()) flush();
    }
};

template <typename Release>
std::vector<BYTE> copyAndRelease(const BYTE* raw, size_t bytes, Release release) {
    std::vector<BYTE> packet;
    try {
        packet.assign(raw, raw + bytes);
    } catch (...) {
        // The caller still owns the WASAPI buffer when copying fails.
        release();
        throw;
    }
    check(release(), "Release capture buffer");
    return packet;
}

bool packetDiscontinuity(DWORD flags, bool havePacket) {
    // The initial packet establishes the clock; there is no earlier audio to lose.
    return havePacket && (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY);
}

void writer(Session& session) {
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    for (;;) {
        Message message;
        {
            std::unique_lock<std::mutex> lock(session.mutex);
            session.changed.wait(lock, [&] { return session.finished || !session.queue.empty(); });
            if (session.queue.empty() && session.finished) break;
            message = std::move(session.queue.front());
            session.queue.pop_front();
            session.bytes -= message.payload.size();
            if (message.track >= 0) session.durations[message.track] -= message.duration;
        }
        if (!frame(output, message.header, message.payload)) { session.stop = true; break; }
    }
    session.writerDone = true;
    session.changed.notify_all();
}

// Accept precisely one JSON object/string field; do not substring-match commands.
// Unknown/malformed/oversized input never changes capture state.
std::string parseCommand(const std::string& line) {
    size_t index = 0;
    auto whitespace = [&] { while (index < line.size() &&
        (line[index] == ' ' || line[index] == '\t' || line[index] == '\r')) ++index; };
    auto token = [&](const char* text) {
        whitespace();
        size_t length = std::strlen(text);
        if (line.compare(index, length, text) != 0) return false;
        index += length;
        return true;
    };
    if (!token("{") || !token("\"command\"") || !token(":")) return {};
    whitespace();
    if (index >= line.size() || line[index++] != '"') return {};
    size_t start = index;
    while (index < line.size() && line[index] >= 'a' && line[index] <= 'z') ++index;
    auto command = line.substr(start, index - start);
    if (!token("\"") || !token("}")) return {};
    whitespace();
    if (index != line.size() || (command != "pause" && command != "resume" && command != "stop")) return {};
    return command;
}
void controls(Session& session) {
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    std::string line;
    bool oversized = false;
    char buffer[256];
    while (!session.stop) {
        DWORD read = 0;
        if (!ReadFile(input, buffer, sizeof(buffer), &read, nullptr) || !read) {
            { std::lock_guard<std::mutex> lock(session.mutex); session.stop = true; }
            session.changed.notify_all();
            break;
        }
        for (DWORD i = 0; i < read; ++i) {
            if (buffer[i] == '\n') {
                auto command = oversized ? std::string() : parseCommand(line);
                line.clear();
                oversized = false;
                {
                    // Waiters test these flags under the mutex; changing them
                    // outside it can lose the wakeup for a full backoff delay.
                    std::lock_guard<std::mutex> lock(session.mutex);
                    if (command == "stop") session.stop = true;
                    else if (command == "pause") {
                        if (!session.paused.exchange(true)) ++session.pauseEpoch;
                    } else if (command == "resume") {
                        if (session.paused.exchange(false)) ++session.pauseEpoch;
                    }
                }
                session.changed.notify_all();
            } else if (!oversized) {
                if (line.size() == 4096) { oversized = true; line.clear(); }
                else line.push_back(buffer[i]);
            }
        }
    }
    session.controlDone = true;
}
struct Selection {
    int track;
    std::wstring value;
    bool followsDefault() const { return value == L"default:multimedia" || value == L"default:communications"; }
    ERole role() const { return value == L"default:communications" ? eCommunications : eMultimedia; }
    EDataFlow flow() const { return track == 0 ? eCapture : eRender; }
    const char* name() const { return track == 0 ? "microphone" : "system"; }
};
void healthEvent(Session& session, const Selection& selected,
                 const capture_health::Watchdog& health,
                 const capture_health::RecoveryBudget& recovery,
                 const char* state, const char* reason) {
    session.event("capture_health", ",\"track\":" + quote(selected.name())
        + ",\"timestamp\":" + number(session.now())
        + ",\"state\":" + quote(state) + ",\"reason\":" + quote(reason)
        + ",\"packets\":" + std::to_string(health.packets)
        + ",\"frames\":" + std::to_string(health.frames)
        + ",\"last_packet_age\":" + number(std::max(0.0, session.now() - health.lastPacket))
        + ",\"recovery_attempt\":" + std::to_string(recovery.attempts), selected.track);
}
void recoveryWait(Session& session, int milliseconds) {
    std::unique_lock<std::mutex> lock(session.mutex);
    session.changed.wait_for(lock, std::chrono::milliseconds(milliseconds), [&] {
        return session.stop.load();
    });
}
// attempt() returns true to reopen at once (a followed default changed),
// false when capture ended normally. switched() reports a different default
// endpoint that is ready now; following it is a device change rather than an
// interruption, so it spends no retry and records no loss.
template<typename Attempt, typename Switched, typename Wait>
void recoverSource(Session& session, const Selection& selected,
                   capture_health::RecoveryBudget& recovery,
                   capture_health::Watchdog& health, Attempt attempt,
                   Switched switched, Wait wait) {
    bool missing = false;
    uint64_t packetsAtFailure = 0;
    while (!session.stop) {
        // A paused recording must not spend its reconnect budget while the
        // user is intentionally away from the endpoint.
        if (session.paused && !session.stop)
            healthEvent(session, selected, health, recovery, "paused", "recovery_paused");
        while (session.paused && !session.stop) wait(100);
        if (session.stop) return;
        try {
            if (!attempt()) return;
        } catch (const AudioFailure& error) {
            if (session.stop) return;
            if (!recoverable(error.result)) throw;
            if (switched()) continue;
            int delay = recovery.nextDelay();
            // Waiting on an absent endpoint is one interruption, however
            // many reconnect polls it takes.
            if (!missing || health.packets != packetsAtFailure)
                session.gap(selected.track, session.now(), "capture_interruption_reopening");
            missing = error.endpointMissing;
            packetsAtFailure = health.packets;
            healthEvent(session, selected, health, recovery,
                missing ? "disconnected" : "recovering", error.what());
            wait(delay);
        }
    }
}
struct Format {
    unsigned rate, channels, bits, validBits, stride;
    bool floating;
    explicit Format(const WAVEFORMATEX* wave) {
        rate = wave->nSamplesPerSec;
        channels = wave->nChannels;
        bits = wave->wBitsPerSample;
        validBits = bits;
        unsigned tag = wave->wFormatTag;
        if (tag == WAVE_FORMAT_EXTENSIBLE) {
            if (wave->cbSize < sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX))
                throw std::runtime_error("Truncated extensible endpoint format");
            auto extended = reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(wave);
            validBits = extended->Samples.wValidBitsPerSample;
            if (!validBits) validBits = bits;
            if (IsEqualGUID(extended->SubFormat, KSDATAFORMAT_SUBTYPE_IEEE_FLOAT)) tag = WAVE_FORMAT_IEEE_FLOAT;
            else if (IsEqualGUID(extended->SubFormat, KSDATAFORMAT_SUBTYPE_PCM)) tag = WAVE_FORMAT_PCM;
            else throw std::runtime_error("Unsupported endpoint subformat; select a PCM/float endpoint");
        }
        floating = tag == WAVE_FORMAT_IEEE_FLOAT;
        if (rate < 8000 || rate > 192000 || !channels || channels > 8
            || (floating ? bits != 32 && bits != 64 : tag != WAVE_FORMAT_PCM
                || (bits != 8 && bits != 16 && bits != 24 && bits != 32))
            || !validBits || validBits > bits || (floating && validBits != bits))
            throw std::runtime_error("Unsupported endpoint PCM format (requires 8000-192000 Hz, 1-8 channels, PCM 8/16/24/32 or float 32/64); select a compatible endpoint format in Windows Sound settings");
        stride = bits / 8;
        if (wave->nBlockAlign != channels * stride)
            throw std::runtime_error("Invalid endpoint block alignment");
    }
    float sample(const BYTE* bytes) const {
        if (floating) {
            double n;
            if (bits == 32) { float f; std::memcpy(&f, bytes, 4); n = f; }
            else std::memcpy(&n, bytes, 8);
            // Float mix samples can legitimately exceed +/-1; retain headroom.
            if (!std::isfinite(n) || std::abs(n) > std::numeric_limits<float>::max())
                throw std::runtime_error("Endpoint delivered invalid floating-point samples; source recording interrupted, select a compatible endpoint and retry");
            return static_cast<float>(n);
        }
        if (bits == 8) {
            int sample = (static_cast<int>(bytes[0]) - 128) / (1 << (bits - validBits));
            return static_cast<float>(sample / std::ldexp(1.0, static_cast<int>(validBits) - 1));
        }
        uint32_t word = 0;
        for (unsigned i = 0; i < stride; ++i) word |= static_cast<uint32_t>(bytes[i]) << (8 * i);
        int64_t signedValue = word;
        if (word & (uint32_t{1} << (bits - 1))) signedValue -= int64_t{1} << bits;
        // Valid PCM bits are left-aligned in the container (WAVEFORMATEXTENSIBLE).
        signedValue /= int64_t{1} << (bits - validBits);
        return static_cast<float>(signedValue / std::ldexp(1.0, static_cast<int>(validBits) - 1));
    }
};

void healthSelfTest() {
    auto require = [](bool valid, const char* scenario) {
        if (!valid) throw std::runtime_error(std::string("Capture health self-test: ") + scenario);
    };
    capture_health::Watchdog quiet;
    quiet.reset(0);
    for (unsigned i = 1; i <= 240; ++i) {
        require(!quiet.inspect(i * 0.5, true, true, true, 0), "quiet loopback must not restart");
        require(!quiet.inspect(i * 0.5, true, false, false, 0), "unknown loopback is not proof of starvation");
    }
    require(quiet.packets == 0 && quiet.frames == 0, "quiet loopback must not fabricate PCM");
    capture_health::Watchdog race;
    race.reset(0);
    race.emptyAdvertised(0.1);
    race.noPending();
    require(!race.inspect(5, true, false, false, 0), "one empty-buffer race is harmless");
    capture_health::Watchdog empty;
    empty.reset(0);
    const char* reason = nullptr;
    for (unsigned i = 0; i <= 6; ++i) {
        empty.emptyAdvertised(i * 0.5);
        reason = empty.inspect(i * 0.5, true, false, false, 0);
        if (i < 6) require(!reason, "zero-frame grace must be honored");
    }
    require(reason && std::string(reason) == "capture_interruption_zero_frames", "persistent zero-frame failure");
    capture_health::Watchdog microphone;
    microphone.reset(0);
    microphone.inspect(1, false, true, false, 0);
    microphone.inspect(2, false, true, false, 0);
    require(microphone.inspect(3, false, true, false, 0) != nullptr, "microphone clock without packets");
    microphone.reset(300);
    require(!microphone.inspect(300.5, false, true, false, 0), "resume grants a new watchdog grace");
    microphone.packet(303, 480);
    require(!microphone.inspect(303, false, true, true, 0), "silent nonzero packets are healthy");
    capture_health::Watchdog render;
    render.reset(0);
    require(!render.inspect(2, true, true, true, .1f), "one meter spike is insufficient");
    require(!render.inspect(2.5, true, true, true, .1f), "sustained activity grace");
    reason = render.inspect(3, true, true, true, .1f);
    require(reason && std::string(reason) == "capture_interruption_render_starvation", "active render without captured packets");
    capture_health::Watchdog spike;
    spike.reset(0);
    spike.inspect(3, true, true, true, .1f);
    require(!spike.inspect(3.5, true, true, true, 0), "meter silence clears activity suspicion");
    require(!spike.inspect(4, true, false, true, .1f), "unavailable stream clock is not corroboration");

    Session recovered;
    Selection same{1, L"synthetic-fixed-endpoint"};
    capture_health::Watchdog health;
    capture_health::RecoveryBudget budget;
    std::vector<int> waits;
    unsigned opens = 0;
    uint64_t sequence = 0;
    std::vector<BYTE> pcm(480 * sizeof(float), 17);
    recoverSource(recovered, same, budget, health, [&]() {
        ++opens;
        AudioBatch batch{recovered, 1, 48000, 1, 0, sequence};
        batch.append(opens * .01, 480, pcm);
        batch.flush();
        if (opens == 1) throw AudioFailure(AUDCLNT_E_DEVICE_INVALIDATED, "synthetic same-device reconnect");
        return false;
    }, [] { return false; }, [&](int delay) { waits.push_back(delay); });
    require(opens == 2 && waits == std::vector<int>{250}, "same endpoint retries without changing selection");
    require(sequence == 2 && recovered.queue.front().payload == pcm
        && recovered.queue.back().payload == pcm, "recovery preserves both PCM batches and sequence");
    require(recovered.queue[1].header.find("capture_interruption_reopening") != std::string::npos
        && recovered.queue[2].header.find("recovering") != std::string::npos,
        "recovery interruption and health evidence are retained");

    Session persistent;
    capture_health::RecoveryBudget unlimited;
    waits.clear();
    opens = 0;
    recoverSource(persistent, same, unlimited, health, [&]() -> bool {
        ++opens;
        throw AudioFailure(AUDCLNT_E_RESOURCES_INVALIDATED, "synthetic persistent failure");
    }, [] { return false; }, [&](int delay) {
        waits.push_back(delay);
        if (waits.size() == 9) persistent.stop = true;
    });
    require(opens == 9 && waits == std::vector<int>({250, 500, 1000, 2000, 4000, 4000, 4000, 4000, 4000})
        && unlimited.attempts == 6, "persistent failures keep a bounded four-second cadence");
    require(persistent.queue.back().header.find("\"recovery_attempt\":6") != std::string::npos,
        "the reported attempt saturates at six");

    Session unplugged;
    capture_health::RecoveryBudget unpluggedBudget;
    capture_health::Watchdog unpluggedHealth;
    opens = 0;
    recoverSource(unplugged, same, unpluggedBudget, unpluggedHealth, [&]() -> bool {
        ++opens;
        if (opens == 4) {
            unpluggedHealth.packet(1, 480);
            throw AudioFailure(AUDCLNT_E_DEVICE_INVALIDATED, "synthetic second unplug", true);
        }
        if (opens == 6) return false;
        throw AudioFailure(AUDCLNT_E_DEVICE_INVALIDATED, "synthetic missing endpoint", true);
    }, [] { return false; }, [](int) {});
    unsigned unpluggedGaps = 0, disconnectedEvents = 0;
    for (const auto& message : unplugged.queue) {
        if (message.header.find("capture_interruption_reopening") != std::string::npos) ++unpluggedGaps;
        if (message.header.find("\"disconnected\"") != std::string::npos) ++disconnectedEvents;
    }
    require(opens == 6 && unpluggedGaps == 2 && disconnectedEvents == 5 && !unplugged.stop,
        "a missing endpoint is one interruption per outage and keeps waiting");

    Session followed;
    capture_health::RecoveryBudget followedBudget;
    waits.clear();
    opens = 0;
    unsigned switches = 0;
    recoverSource(followed, same, followedBudget, health, [&]() -> bool {
        ++opens;
        if (opens == 1) return true; // the followed default changed while running
        if (opens == 2) throw AudioFailure(AUDCLNT_E_DEVICE_INVALIDATED, "synthetic default removed");
        return false;
    }, [&] { return ++switches == 1; }, [&](int delay) { waits.push_back(delay); });
    require(opens == 3 && switches == 1 && waits.empty() && followedBudget.attempts == 0
        && followed.queue.empty(), "following a new default spends no retry and records no loss");

    Session denied;
    capture_health::RecoveryBudget permanent;
    bool deniedRaised = false;
    try {
        recoverSource(denied, same, permanent, health, []() -> bool {
            throw AudioFailure(E_ACCESSDENIED, "synthetic permission failure");
        }, [] { return false; }, [&](int) { require(false, "permission errors must not retry"); });
    } catch (const AudioFailure& error) { deniedRaised = error.result == E_ACCESSDENIED; }
    require(deniedRaised && permanent.attempts == 0, "permanent errors remain actionable");
    capture_health::RecoveryBudget stable;
    require(stable.nextDelay() == 250, "first recovery delay");
    stable.packet(1, 480, 48000);
    require(stable.nextDelay() == 500, "one packet cannot reset retry budget");
    for (unsigned i = 0; i <= 3001; ++i) stable.packet(2 + i * .01, 480, 48000);
    require(stable.attempts == 0 && stable.nextDelay() == 250, "stable capture restores retry budget");
    Session cancelled;
    capture_health::RecoveryBudget cancelledBudget;
    opens = 0;
    recoverSource(cancelled, same, cancelledBudget, health, [&]() -> bool {
        ++opens;
        throw AudioFailure(AUDCLNT_E_DEVICE_INVALIDATED, "synthetic cancellation");
    }, [] { return false; }, [&](int) { cancelled.stop = true; });
    require(opens == 1 && cancelledBudget.attempts == 1, "stop prevents another reopen during backoff");
    Session interruptedWait;
    auto began = Clock::now();
    std::thread stopper([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        interruptedWait.stop = true;
        interruptedWait.changed.notify_all();
    });
    recoveryWait(interruptedWait, 4000);
    stopper.join();
    require(Clock::now() - began < std::chrono::seconds(1), "stop interrupts the real backoff wait");
}

void selfTestLogic() {
    healthSelfTest();
    auto require = [](bool valid) {
        if (!valid) throw std::runtime_error("Native non-recording self-test failed");
    };
    require(parseCommand(" { \"command\" : \"pause\" } ") == "pause");
    require(parseCommand("{\"command\":\"resume\"}") == "resume");
    require(parseCommand("{\"command\":\"stop\"}\r") == "stop");
    require(parseCommand("{\"command\":\"stop\",\"extra\":1}").empty());
    require(parseCommand("garbage {\"command\":\"stop\"}").empty());
    WAVEFORMATEX wave{};
    wave.wFormatTag = WAVE_FORMAT_PCM;
    wave.nChannels = 1;
    wave.nSamplesPerSec = 48000;
    const BYTE minimum[] = {0, 0, 0, 128};
    for (unsigned bits : {8u, 16u, 24u, 32u}) {
        wave.wBitsPerSample = static_cast<WORD>(bits);
        wave.nBlockAlign = static_cast<WORD>(bits / 8);
        Format format(&wave);
        BYTE bytes[4] = {};
        bytes[bits / 8 - 1] = 128;
        require(format.sample(bytes) == (bits == 8 ? 0.0f : -1.0f));
        if (bits == 32) require(format.sample(minimum) == -1.0f);
    }
    wave.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
    wave.wBitsPerSample = 32;
    wave.nBlockAlign = 4;
    Format format(&wave);
    float input = 1.25f;
    BYTE bytes[4];
    std::memcpy(bytes, &input, 4);
    require(format.sample(bytes) == input);
    input = std::numeric_limits<float>::quiet_NaN();
    std::memcpy(bytes, &input, 4);
    bool rejected = false;
    try { format.sample(bytes); } catch (const std::runtime_error&) { rejected = true; }
    require(rejected);
    WAVEFORMATEXTENSIBLE extended{};
    extended.Format = wave;
    extended.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
    extended.Format.cbSize = sizeof(WAVEFORMATEXTENSIBLE) - sizeof(WAVEFORMATEX);
    extended.SubFormat = KSDATAFORMAT_SUBTYPE_PCM;
    extended.Samples.wValidBitsPerSample = 24;
    require(Format(&extended.Format).sample(minimum) == -1.0f);
    require(quote("\"\\\n") == "\"\\\"\\\\\\u000a\"");
    require(!packetDiscontinuity(AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, false));
    require(packetDiscontinuity(AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY, true));
    auto continuous = nativePacketFields(AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY,
        480, 48000, 1000, true, 1000, true, 10.05, 10.01);
    require(continuous.find("\"buffer_flags\":1") != std::string::npos);
    require(continuous.find("\"device_position\":1000") != std::string::npos);
    require(continuous.find("\"expected_device_position\":1000") != std::string::npos);
    require(continuous.find("\"qpc_timestamp\":10.05") != std::string::npos);
    require(continuous.find("\"sample_timestamp\":10.01") != std::string::npos);
    auto jump = nativePacketFields(AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY,
        480, 48000, 1010, true, 1000, true);
    require(jump.find("\"device_position\":1010") != std::string::npos);
    require(jump.find("\"expected_device_position\":1000") != std::string::npos);
    auto reset = nativePacketFields(0, 480, 48000, 2000, false, 0, true);
    require(reset.find("\"expected_device_position\":null") != std::string::npos);
    auto silent = nativePacketFields(AUDCLNT_BUFFERFLAGS_SILENT,
        480, 48000, 3000, true, 3000, true);
    require(silent.find("\"buffer_flags\":2") != std::string::npos);
    require(silent.find("\"device_position\":3000") != std::string::npos);
    auto timestampInvalid = nativePacketFields(AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR,
        480, 48000, 4000, true, 4000, false);
    require(timestampInvalid.find("\"buffer_flags\":4") != std::string::npos);
    require(timestampInvalid.find("\"device_position\":null") != std::string::npos);
    require(timestampInvalid.find("\"expected_device_position\":null") != std::string::npos);
    UINT64 expectedPosition = 0;
    require(nextExpectedDevicePosition(1000, 480, expectedPosition) && expectedPosition == 1480);
    require(nextExpectedDevicePosition(expectedPosition, 480, expectedPosition) && expectedPosition == 1960);
    require(nextExpectedDevicePosition(3000, 480, expectedPosition) && expectedPosition == 3480);
    require(!nextExpectedDevicePosition(std::numeric_limits<UINT64>::max(), 1, expectedPosition));
    bool haveTrackedPosition = false;
    auto trackPacket = [&](bool validTimestamp, UINT64 position, UINT32 packetFrames) {
        bool hadPrevious = haveTrackedPosition;
        if (!validTimestamp || !nextExpectedDevicePosition(position, packetFrames, expectedPosition))
            haveTrackedPosition = false;
        else
            haveTrackedPosition = true;
        return hadPrevious;
    };
    require(!trackPacket(true, 1000, 480));
    require(trackPacket(true, 1480, 480));
    trackPacket(false, 0, 480);
    require(!haveTrackedPosition);
    require(!trackPacket(true, 5000, 480));
    require(haveTrackedPosition && expectedPosition == 5480);
    SampleTimeline timeline;
    require(timeline.timestamp(10.0, 1000, 480, 48000, true) == 10.0);
    require(std::abs(timeline.timestamp(10.05, 1480, 480, 48000, true) - 10.01) < 1e-12);
    require(std::abs(timeline.timestamp(10.2, 3000, 480, 48000, true) - 10.2) < 1e-12);
    timeline.reset();
    require(std::abs(timeline.timestamp(20.0, 9000, 480, 48000, true) - 20.0) < 1e-12);
    timeline.reset();
    require(std::abs(timeline.timestamp(20.0, 9000, 480, 48000, false) - 20.0) < 1e-12
        && !timeline.have);
    timeline.reset();
    UINT64 longPosition = 100000;
    double longTimestamp = timeline.timestamp(30.0, longPosition, 480, 48000, true);
    for (unsigned packet = 1; packet <= 1080000; ++packet) {
        longPosition += 480;
        // Alternate a 10 ms QPC step while the endpoint counter remains continuous.
        longTimestamp = timeline.timestamp(30.0 + packet * 0.01
            + (packet % 2 ? 0.01 : 0.0), longPosition, 480, 48000, true);
    }
    require(std::abs(longTimestamp - (30.0 + 1080000 * 0.01)) < 1e-9);
    Session buffered;
    uint64_t sequence = 0;
    AudioBatch batch{buffered, 0, 48000, 1, 0, sequence};
    std::vector<BYTE> pcm(480 * sizeof(float), 1);
    for (unsigned i = 0; i < 100; ++i) batch.append(i * 0.01, 480, pcm);
    require(buffered.queue.size() == 10 && sequence == 10 && batch.frames == 0);
    require(buffered.queue.front().payload.size() == 4800 * sizeof(float));
    require(std::all_of(buffered.queue.front().payload.begin(),
        buffered.queue.front().payload.end(), [](BYTE value) { return value == 1; }));
    Session flagged;
    uint64_t flaggedSequence = 0;
    AudioBatch flaggedBatch{flagged, 0, 48000, 1, 0, flaggedSequence};
    flaggedBatch.append(0.0, 4800, std::vector<BYTE>(4800 * sizeof(float), 0),
        AUDCLNT_BUFFERFLAGS_SILENT);
    require(flagged.queue.size() == 1
        && flagged.queue.front().header.find("\"buffer_flags\":2") != std::string::npos);
    batch.append(1.0, 480, pcm);
    batch.append(1.2, 480, pcm); // Preserve a real 190 ms source gap.
    require(sequence == 11 && batch.frames == 480);
    batch.flush(); // Pause/stop must preserve the short tail.
    require(sequence == 12 && buffered.queue.back().duration == 0.01);
    Session smallPackets;
    for (unsigned i = 0; i < 200; ++i) smallPackets.push({"{}", std::vector<BYTE>(1920), 0, 0.01});
    require(!smallPackets.overflow && smallPackets.queue.size() == 200);
    BYTE source[] = {1, 2, 3, 4};
    bool released = false;
    auto packet = copyAndRelease(source, sizeof(source), [&] {
        released = true;
        return S_OK;
    });
    require(released && packet.size() == sizeof(source)
        && std::equal(packet.begin(), packet.end(), source));
    unsigned releaseCalls = 0;
    bool releaseFailed = false;
    try {
        copyAndRelease(source, sizeof(source), [&] {
            ++releaseCalls;
            return E_FAIL;
        });
    } catch (const std::runtime_error&) {
        releaseFailed = true;
    }
    require(releaseFailed && releaseCalls == 1);
}

void capture(Session& session, Selection selected) {
    try {
        ComScope com;
        auto devices = enumerator();
        uint64_t sequence = 0;
        std::wstring previous;
        capture_health::RecoveryBudget recovery;
        capture_health::Watchdog health;
        auto publishHealth = [&](const char* state, const char* reason) {
            healthEvent(session, selected, health, recovery, state, reason);
        };
        std::wstring switchTarget;
        auto switched = [&]() {
            if (!selected.followsDefault()) return false;
            std::wstring id;
            try {
                id = defaultId(devices.Get(), selected.flow(), selected.role());
            } catch (const AudioFailure&) {
                return false;
            }
            // Each new default gets one immediate open; if that open fails
            // too, the normal backoff applies instead of a tight loop.
            if (id.empty() || id == previous || id == switchTarget) return false;
            switchTarget = id;
            return true;
        };
        auto attempt = [&]() {
            std::wstring id = selected.followsDefault()
                ? defaultId(devices.Get(), selected.flow(), selected.role()) : selected.value;
            if (id.empty())
                throw AudioFailure(AUDCLNT_E_DEVICE_INVALIDATED, "No active OS default endpoint", true);
            ComPtr<IMMDevice> device;
            HRESULT opened = devices->GetDevice(id.c_str(), device.GetAddressOf());
            if (opened == HRESULT_FROM_WIN32(ERROR_NOT_FOUND) || opened == E_NOTFOUND)
                throw AudioFailure(opened, "Selected endpoint is not connected", true);
            check(opened, "Open selected endpoint");
            ComPtr<IMMEndpoint> endpoint;
            check(device.As(&endpoint), "Inspect endpoint direction");
            EDataFlow flow;
            check(endpoint->GetDataFlow(&flow), "Inspect endpoint direction");
            if (flow != selected.flow()) throw std::runtime_error("Selected endpoint has the wrong source direction; refresh devices");
            DWORD state = 0;
            check(device->GetState(&state), "Inspect selected endpoint state");
            if (!(state & DEVICE_STATE_ACTIVE))
                throw AudioFailure(AUDCLNT_E_DEVICE_INVALIDATED, "Selected endpoint is disconnected or disabled", true);
            ComPtr<IAudioClient> client;
            check(device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr,
                reinterpret_cast<void**>(client.GetAddressOf())), "Activate WASAPI endpoint");
            Wave wave;
            check(client->GetMixFormat(&wave.value), "Read endpoint mix format");
            Format format(wave.value);
            DWORD flags = selected.track == 1 ? AUDCLNT_STREAMFLAGS_LOOPBACK : 0;
            check(client->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 2000000, 0,
                wave.value, nullptr), "Initialize shared capture stream");
            ComPtr<IAudioCaptureClient> input;
            check(client->GetService(IID_PPV_ARGS(input.GetAddressOf())), "Open WASAPI capture service");
            ComPtr<IAudioClock> audioClock;
            // Optional observations never prevent otherwise usable capture.
            client->GetService(IID_PPV_ARGS(audioClock.GetAddressOf()));
            ComPtr<IAudioMeterInformation> meter;
            device->Activate(__uuidof(IAudioMeterInformation), CLSCTX_ALL, nullptr,
                reinterpret_cast<void**>(meter.GetAddressOf()));
            session.event("source_changed", ",\"track\":" + quote(selected.name())
                + ",\"endpoint_id\":" + quote(utf8(id))
                + ",\"timestamp\":" + number(session.now()), selected.track);
            if (!previous.empty() && previous != id)
                session.gap(selected.track, session.now(), "default_endpoint_changed");
            previous = id;
            bool running = !session.paused;
            if (running) check(client->Start(), "Start capture stream");
            unsigned epoch = session.pauseEpoch;
            auto nextDefaultCheck = Clock::now() + std::chrono::milliseconds(500);
            auto nextHealthCheck = Clock::now() + std::chrono::milliseconds(500);
            auto nextHealthReport = Clock::now();
            health.reset(session.now());
            std::string healthState;
            bool reopen = false;
            HRESULT failure = S_OK;
            double lastEnd = session.now();
            double lastPacketWallTime = session.now();
            bool idleGap = false;
            bool havePacket = false;
            bool haveExpectedDevicePosition = false;
            UINT64 expectedNextDevicePosition = 0;
            AudioBatch batch{session, selected.track, format.rate, format.channels, 0, sequence};
            SampleTimeline sampleTimeline;
            auto drain = [&](bool tail) {
                HRESULT result = S_OK;
                UINT32 pending = 0;
                result = input->GetNextPacketSize(&pending);
                if (FAILED(result)) return result;
                if (!pending) health.noPending();
#ifdef SNIPVOICE_CAPTURE_FAULT_TESTS
                if (!tail && pending && selected.track == 1 && !session.faultInjected
                        && session.now() >= 3.0
                        && (session.faultScript == L"zero-once" || session.faultScript == L"empty-once")) {
                    health.emptyAdvertised(session.now());
                    if (session.faultScript == L"empty-once") session.faultInjected = true;
                    return AUDCLNT_S_BUFFER_EMPTY;
                }
#endif
                // Drain available packets before the next bounded polling wait.
                while (pending && (tail || !session.stop)) {
                    BYTE* raw = nullptr;
                    UINT32 frames = 0;
                    DWORD bufferFlags = 0;
                    UINT64 devicePosition = 0, qpc100ns = 0;
                    result = input->GetBuffer(&raw, &frames, &bufferFlags, &devicePosition, &qpc100ns);
                    if (FAILED(result)) break;
                    if (!frames) {
                        // BUFFER_EMPTY can race GetNextPacketSize. Microsoft
                        // permits omitting ReleaseBuffer for a zero-size packet.
                        // Only persistent advertised-but-empty reads are faults.
                        health.emptyAdvertised(session.now());
                        break;
                    }
                    if (frames > format.rate * 2u) {
                        input->ReleaseBuffer(frames);
                        throw std::runtime_error("Native packet exceeds the two-second safety limit");
                    }
                    double timestamp = static_cast<double>(qpc100ns) / 10000000.0 - session.origin;
                    bool silent = (bufferFlags & AUDCLNT_BUFFERFLAGS_SILENT) != 0;
                    bool invalidBuffer = !silent && !raw;
                    bool haveExpectedBefore = haveExpectedDevicePosition;
                    UINT64 expectedBefore = expectedNextDevicePosition;
                    std::vector<BYTE> packet;
                    // Release WASAPI's buffer before any diagnostic event,
                    // conversion, or transport work. This keeps discontinuity
                    // handling from holding the endpoint buffer while the
                    // bounded queue is locked or flushed.
                    if (!invalidBuffer && !silent) {
                        try {
                            packet = copyAndRelease(raw, static_cast<size_t>(frames)
                                * format.channels * format.stride,
                                [&] { return input->ReleaseBuffer(frames); });
                        } catch (...) { batch.flush(); throw; }
                    } else {
                        result = input->ReleaseBuffer(frames);
                        if (FAILED(result)) break;
                    }
                    bool timestampInvalid = (bufferFlags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR)
                        || !std::isfinite(timestamp) || timestamp < 0;
                    if (timestampInvalid) {
                        sampleTimeline.reset();
                        haveExpectedDevicePosition = false;
                        batch.flush();
                        session.nativeGap(selected.track, session.now(), "native_timestamp_error",
                            nativePacketFields(bufferFlags, frames, format.rate, devicePosition,
                                haveExpectedBefore, expectedBefore, false, timestamp));
                        havePacket = true;
                        // Never fabricate a device timestamp for unclocked samples.
                    } else {
                        double packetTimestamp = sampleTimeline.timestamp(
                            timestamp, devicePosition, frames, format.rate, !invalidBuffer);
                        if (invalidBuffer) sampleTimeline.reset();
                        if (!nextExpectedDevicePosition(devicePosition, frames, expectedNextDevicePosition))
                            haveExpectedDevicePosition = false;
                        else
                            haveExpectedDevicePosition = true;
                        if (idleGap) {
                            session.gap(selected.track, packetTimestamp, "native_packets_resumed");
                            idleGap = false;
                        }
                        lastEnd = packetTimestamp + static_cast<double>(frames) / format.rate;
                        if (packetDiscontinuity(bufferFlags, havePacket)) {
                            batch.flush();
                            session.nativeGap(selected.track, packetTimestamp, "native_discontinuity",
                                nativePacketFields(bufferFlags, frames, format.rate, devicePosition,
                                    haveExpectedBefore, expectedBefore, true,
                                    timestamp, packetTimestamp));
                        }
                        havePacket = true;
                        if (invalidBuffer) {
                            batch.flush();
                            session.gap(selected.track, packetTimestamp, "invalid_native_buffer");
                        }
                        // ceiling: each copied block is <=100 ms and <=4 MiB.
                        unsigned maximum = std::min<unsigned>(format.rate / 10,
                            static_cast<unsigned>(kMaxPayload / (format.channels * sizeof(float))));
                        maximum = std::max(1u, maximum);
                        try {
                            for (UINT32 offset = 0; offset < frames && !invalidBuffer && (tail || !session.stop); offset += maximum) {
                                unsigned count = std::min<unsigned>(maximum, frames - offset);
                                Message block;
                                block.track = selected.track;
                                block.duration = static_cast<double>(count) / format.rate;
                                block.payload.resize(static_cast<size_t>(count) * format.channels * sizeof(float));
                                for (size_t sample = 0; sample < static_cast<size_t>(count) * format.channels; ++sample) {
                                    float value = silent ? 0.0f : format.sample(packet.data()
                                        + (static_cast<size_t>(offset) * format.channels + sample) * format.stride);
                                    std::memcpy(block.payload.data() + sample * sizeof(float), &value, sizeof(float));
                                }
                                batch.append(packetTimestamp + static_cast<double>(offset) / format.rate,
                                    count, block.payload, offset == 0 ? bufferFlags : 0);
                            }
                            if (!invalidBuffer) {
                                double observed = session.now();
                                lastPacketWallTime = observed;
                                health.packet(observed, frames);
                                recovery.packet(observed, frames, format.rate);
                            }
                        } catch (...) { batch.flush(); throw; }
                    }
                    result = input->GetNextPacketSize(&pending);
                    if (FAILED(result)) break;
                    if (!pending) health.noPending();
                }
                return result;
            };
            try {
            while (!session.stop && !reopen) {
                if (session.pauseEpoch != epoch || running == session.paused.load()) {
                    unsigned currentEpoch = session.pauseEpoch;
                    bool pause = session.paused;
                    if (running) {
                        check(client->Stop(), "Pause capture stream");
                        check(drain(true), "Preserve paused capture tail");
                        batch.flush();
                        check(client->Reset(), "Reset paused capture stream");
                        running = false;
                        havePacket = false;
                        haveExpectedDevicePosition = false;
                        sampleTimeline.reset();
                    }
                    session.gap(selected.track, session.now(), pause ? "paused" : "resumed");
                    lastEnd = session.now();
                    lastPacketWallTime = lastEnd;
                    health.reset(session.now());
                    idleGap = false;
                    if (!pause) { check(client->Start(), "Resume capture stream"); running = true; }
                    epoch = currentEpoch;
                }
                if (running && selected.followsDefault() && Clock::now() >= nextDefaultCheck) {
                    if (defaultId(devices.Get(), selected.flow(), selected.role()) != id) {
                        reopen = true;
                        break;
                    }
                    nextDefaultCheck = Clock::now() + std::chrono::milliseconds(500);
                }
                if (running) {
#ifdef SNIPVOICE_CAPTURE_FAULT_TESTS
                    if (selected.track == 1 && session.now() >= 3.0
                            && (session.faultScript == L"invalidate-persistent"
                                || (session.faultScript == L"invalidate-once" && !session.faultInjected))) {
                        session.faultInjected = true;
                        failure = AUDCLNT_E_DEVICE_INVALIDATED;
                        break;
                    }
#endif
                    failure = drain(false);
                    if (FAILED(failure)) break;
                    if (session.now() - lastPacketWallTime > 0.1) batch.flush();
                    // A silent loopback engine may deliver no packets at all. Keep
                    // elapsed time explicit without inventing native clocked PCM.
                    if (!idleGap && session.now() - lastPacketWallTime > 0.5) {
                        session.gap(selected.track, lastEnd, "no_native_packets");
                        idleGap = true;
                    }
                }
                if (!running && (healthState != "paused" || Clock::now() >= nextHealthReport)) {
                    publishHealth("paused", "packet_watchdog");
                    healthState = "paused";
                    nextHealthReport = Clock::now() + std::chrono::seconds(5);
                }
                if (running && Clock::now() >= nextHealthCheck) {
                    check(device->GetState(&state), "Inspect capture endpoint health");
                    if (!(state & DEVICE_STATE_ACTIVE))
                        throw AudioFailure(AUDCLNT_E_DEVICE_INVALIDATED, "Capture endpoint disconnected", true);
                    UINT64 clockPosition = 0;
                    HRESULT clockResult = audioClock
                        ? audioClock->GetPosition(&clockPosition, nullptr) : E_NOINTERFACE;
                    float peak = 0;
                    HRESULT meterResult = meter ? meter->GetPeakValue(&peak) : E_NOINTERFACE;
                    bool meterValid = SUCCEEDED(meterResult) && std::isfinite(peak) && peak >= 0 && peak <= 1;
                    if (FAILED(clockResult) && recoverable(clockResult))
                        check(clockResult, "Read capture stream clock");
                    if (FAILED(meterResult) && recoverable(meterResult))
                        check(meterResult, "Read endpoint meter");
                    double observed = session.now();
                    if (running) {
                        const char* reason = health.inspect(observed, selected.track == 1,
                            SUCCEEDED(clockResult), meterValid, peak);
                        if (reason) {
                            session.gap(selected.track, observed, reason);
#ifdef SNIPVOICE_CAPTURE_FAULT_TESTS
                            if (session.faultScript == L"zero-once") session.faultInjected = true;
#endif
                            throw AudioFailure(AUDCLNT_E_RESOURCES_INVALIDATED, reason);
                        }
                    }
                    std::string current = !running ? "paused"
                        : observed - health.lastPacket < 0.5 ? "receiving"
                        : selected.track == 1 && meterValid
                            && peak <= capture_health::kActivePeak ? "quiet" : "waiting";
                    if (current != healthState || Clock::now() >= nextHealthReport) {
                        publishHealth(current.c_str(), "packet_watchdog");
                        healthState = current;
                        nextHealthReport = Clock::now() + std::chrono::seconds(5);
                    }
                    nextHealthCheck = Clock::now() + std::chrono::milliseconds(500);
                }
                std::unique_lock<std::mutex> lock(session.mutex);
                session.changed.wait_for(lock, std::chrono::milliseconds(10), [&] {
                    return session.stop.load() || session.pauseEpoch.load() != epoch;
                });
            }
            if (running) {
                HRESULT stopped = client->Stop();
                if (SUCCEEDED(failure)) failure = stopped;
                if (SUCCEEDED(failure)) failure = drain(true);
            }
            batch.flush();
            if (FAILED(failure)) {
                check(failure, "Read selected source (recording on other sources continues)");
            }
            } catch (...) {
                // Preserve the last sub-100ms batch before abandoning this
                // client. Never drain a client after a failed buffer operation.
                batch.flush();
                if (running) client->Stop();
                throw;
            }
            return reopen;
        };
        recoverSource(session, selected, recovery, health, attempt, switched,
            [&](int delay) { recoveryWait(session, delay); });
    } catch (const std::exception& error) {
        session.error(selected.track, error.what());
    }
    if (--session.alive == 0) session.stop = true;
    session.changed.notify_all();
}

int wmain(int argc, wchar_t** argv) {
    try {
        bool selfTest = false, list = false, record = false;
#ifdef SNIPVOICE_CAPTURE_FAULT_TESTS
        std::wstring faultScript;
#endif
        std::wstring sources = L"both", microphone = L"default:multimedia", system = L"default:multimedia";
        int64_t generation = 0;
        for (int i = 1; i < argc; ++i) {
            std::wstring option(argv[i]);
            if (option == L"--self-test") selfTest = true;
            else if (option == L"--list") list = true;
            else if (option == L"--capture") record = true;
#ifdef SNIPVOICE_CAPTURE_FAULT_TESTS
            else if (option == L"--fault-script" && i + 1 < argc) {
                faultScript = argv[++i];
                if (faultScript != L"invalidate-once" && faultScript != L"invalidate-persistent"
                        && faultScript != L"zero-once" && faultScript != L"empty-once")
                    throw std::runtime_error("Unknown test fault script");
            }
#endif
            else if ((option == L"--sources" || option == L"--microphone" || option == L"--system"
                      || option == L"--generation") && i + 1 < argc) {
                std::wstring value(argv[++i]);
                if (option == L"--sources") sources = value;
                else if (option == L"--microphone") microphone = value;
                else if (option == L"--system") system = value;
                else {
                    size_t consumed = 0;
                    generation = std::stoll(value, &consumed);
                    if (consumed != value.size() || generation < 0) throw std::runtime_error("Generation must be a nonnegative integer");
                }
            } else throw std::runtime_error("Unknown or incomplete argument");
        }
        if (static_cast<int>(selfTest) + static_cast<int>(list) + static_cast<int>(record) != 1)
            throw std::runtime_error("Choose exactly one of --self-test, --list, or --capture");
#ifdef SNIPVOICE_CAPTURE_FAULT_TESTS
        if (!faultScript.empty() && !record)
            throw std::runtime_error("Test fault scripts require capture mode");
#endif
        HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
        if (selfTest) {
            // No COM, endpoint enumeration, or audio activation in package probes.
            selfTestLogic();
            return frame(output, "{\"type\":\"ready\",\"version\":1,\"generation\":0}")
                && frame(output, "{\"type\":\"stopped\",\"generation\":0}") ? 0 : 2;
        }
        if (list) { ComScope com; return frame(output, listDevices()) ? 0 : 2; }
        if (sources != L"both" && sources != L"microphone" && sources != L"system")
            throw std::runtime_error("Sources must be both, microphone, or system");
        if (microphone.empty() || system.empty()) throw std::runtime_error("Endpoint selection cannot be empty");
        Session session;
#ifdef SNIPVOICE_CAPTURE_FAULT_TESTS
        session.faultScript = faultScript;
#endif
        session.generation = generation;
        LARGE_INTEGER counter, frequency;
        if (!QueryPerformanceCounter(&counter) || !QueryPerformanceFrequency(&frequency) || frequency.QuadPart <= 0)
            throw std::runtime_error("Windows native performance clock is unavailable");
        session.origin = static_cast<double>(counter.QuadPart) / frequency.QuadPart;
        session.push({session.prefix("ready") + ",\"version\":1}"});
        session.alive = sources == L"both" ? 2 : 1;
        std::vector<std::thread> workers;
        workers.reserve(2);
        std::thread outputThread(writer, std::ref(session));
        std::thread controlThread;
        try {
            controlThread = std::thread(controls, std::ref(session));
            if (sources != L"system") workers.emplace_back(capture, std::ref(session), Selection{0, microphone});
            if (sources != L"microphone") workers.emplace_back(capture, std::ref(session), Selection{1, system});
        } catch (const std::exception& error) {
            session.error(sources == L"system" ? 1 : 0, std::string("Create capture workers: ") + error.what());
            session.stop = true;
            session.changed.notify_all();
        }
        for (auto& worker : workers) worker.join();
        session.stop = true;
        // Stop can race the control reader just before ReadFile. Repeat cancellation
        // until the reader acknowledges exit, bounded independently of stdin EOF.
        auto controlDeadline = Clock::now() + std::chrono::seconds(2);
        while (controlThread.joinable() && !session.controlDone && Clock::now() < controlDeadline) {
            CancelSynchronousIo(controlThread.native_handle());
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        if (controlThread.joinable() && !session.controlDone) {
            // OS I/O cancellation failed: terminate the isolated helper, never leave
            // an orphan thread running with references into a destroyed session.
            ExitProcess(3);
        }
        if (controlThread.joinable()) controlThread.join();
        session.event("stopped", session.overflow ? ",\"reason\":\"transport_overflow\"" : "");
        { std::lock_guard<std::mutex> lock(session.mutex); session.finished = true; }
        session.changed.notify_all();
        auto deadline = Clock::now() + std::chrono::seconds(2);
        while (!session.writerDone && Clock::now() < deadline)
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        if (!session.writerDone) {
            session.stop = true;
            CancelSynchronousIo(outputThread.native_handle());
            auto cancelDeadline = Clock::now() + std::chrono::seconds(1);
            while (!session.writerDone && Clock::now() < cancelDeadline) {
                CancelSynchronousIo(outputThread.native_handle());
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            }
            if (!session.writerDone) ExitProcess(3);
        }
        outputThread.join();
        return session.overflow ? 3 : 0;
    } catch (const std::exception& error) {
        std::cerr << "Snipvoice Windows capture: " << error.what() << '\n';
        return 1;
    }
}
