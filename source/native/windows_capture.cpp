// Owned WASAPI helper. stdout is exclusively the version-1 framed protocol.
// https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording
// GetBuffer's QPC timestamp is already in 100 ns units, not raw QPC ticks.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
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

using Microsoft::WRL::ComPtr;
using Clock = std::chrono::steady_clock;
constexpr size_t kMaxHeader = 64 * 1024;
constexpr size_t kMaxPayload = 4 * 1024 * 1024;
// ceiling: two seconds per track and 8 MiB total queued PCM; upgrade only with
// measured disk/pipe latency requirements. Overflow is a visible fatal gap.
constexpr size_t kQueueBytes = 8 * 1024 * 1024;
constexpr double kQueueSeconds = 2.0;

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
void check(HRESULT hr, const char* action) {
    if (SUCCEEDED(hr)) return;
    std::ostringstream message;
    message << action << " failed (HRESULT 0x" << std::hex
            << static_cast<unsigned long>(hr)
            << "). Check the selected endpoint, Windows microphone privacy access, "
               "and the Windows Audio service; refresh device selection and retry.";
    throw std::runtime_error(message.str());
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
            || queue.size() >= 128)) {
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
        // ceiling: 128 queued source events/audio blocks, plus one terminal event.
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
    void error(int track, const std::string& message) {
        event("source_error", ",\"track\":" + quote(track == 0 ? "microphone" : "system")
            + ",\"timestamp\":" + number(now()) + ",\"message\":" + quote(message), track);
    }
};
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
            session.stop = true;
            break;
        }
        for (DWORD i = 0; i < read; ++i) {
            if (buffer[i] == '\n') {
                auto command = oversized ? std::string() : parseCommand(line);
                line.clear();
                oversized = false;
                if (command == "stop") session.stop = true;
                else if (command == "pause") {
                    if (!session.paused.exchange(true)) ++session.pauseEpoch;
                } else if (command == "resume") {
                    if (session.paused.exchange(false)) ++session.pauseEpoch;
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

void selfTestLogic() {
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
}

void capture(Session& session, Selection selected) {
    try {
        ComScope com;
        auto devices = enumerator();
        uint64_t sequence = 0;
        std::wstring previous;
        while (!session.stop) {
            std::wstring id = selected.followsDefault()
                ? defaultId(devices.Get(), selected.flow(), selected.role()) : selected.value;
            if (id.empty()) throw std::runtime_error("No active OS default endpoint; connect a device and start a new recording");
            ComPtr<IMMDevice> device;
            check(devices->GetDevice(id.c_str(), device.GetAddressOf()), "Open selected endpoint");
            ComPtr<IMMEndpoint> endpoint;
            check(device.As(&endpoint), "Inspect endpoint direction");
            EDataFlow flow;
            check(endpoint->GetDataFlow(&flow), "Inspect endpoint direction");
            if (flow != selected.flow()) throw std::runtime_error("Selected endpoint has the wrong source direction; refresh devices");
            DWORD state = 0;
            check(device->GetState(&state), "Inspect selected endpoint state");
            if (!(state & DEVICE_STATE_ACTIVE)) throw std::runtime_error("Selected endpoint is disconnected or disabled; refresh devices");
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
            session.event("source_changed", ",\"track\":" + quote(selected.name())
                + ",\"endpoint_id\":" + quote(utf8(id)), selected.track);
            if (!previous.empty()) session.gap(selected.track, session.now(), "default_endpoint_changed");
            previous = id;
            bool running = !session.paused;
            if (running) check(client->Start(), "Start capture stream");
            unsigned epoch = session.pauseEpoch;
            auto nextDefaultCheck = Clock::now() + std::chrono::milliseconds(500);
            bool reopen = false;
            HRESULT failure = S_OK;
            double lastEnd = session.now();
            bool idleGap = false;
            auto drain = [&](bool tail) {
                HRESULT result = S_OK;
                UINT32 pending = 0;
                result = input->GetNextPacketSize(&pending);
                if (FAILED(result)) return result;
                // Drain available packets before the next bounded polling wait.
                while (pending && (tail || !session.stop)) {
                    BYTE* raw = nullptr;
                    UINT32 frames = 0;
                    DWORD bufferFlags = 0;
                    UINT64 devicePosition = 0, qpc100ns = 0;
                    result = input->GetBuffer(&raw, &frames, &bufferFlags, &devicePosition, &qpc100ns);
                    if (FAILED(result)) break;
                    if (!frames) break;
                    if (frames > format.rate * 2u) {
                        input->ReleaseBuffer(frames);
                        throw std::runtime_error("Native packet exceeds the two-second safety limit");
                    }
                    double timestamp = static_cast<double>(qpc100ns) / 10000000.0 - session.origin;
                    if ((bufferFlags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR) || !std::isfinite(timestamp) || timestamp < 0) {
                        session.gap(selected.track, session.now(), "native_timestamp_error");
                        // Never fabricate a device timestamp for unclocked samples.
                        result = input->ReleaseBuffer(frames);
                        if (FAILED(result)) break;
                    } else {
                        if (idleGap) {
                            session.gap(selected.track, timestamp, "native_packets_resumed");
                            idleGap = false;
                        }
                        lastEnd = timestamp + static_cast<double>(frames) / format.rate;
                        if (bufferFlags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY)
                            session.gap(selected.track, timestamp, "native_discontinuity");
                        bool silent = (bufferFlags & AUDCLNT_BUFFERFLAGS_SILENT) != 0;
                        bool invalidBuffer = !silent && !raw;
                        if (invalidBuffer) session.gap(selected.track, timestamp, "invalid_native_buffer");
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
                                    float value = silent ? 0.0f : format.sample(raw
                                        + (static_cast<size_t>(offset) * format.channels + sample) * format.stride);
                                    std::memcpy(block.payload.data() + sample * sizeof(float), &value, sizeof(float));
                                }
                                block.header = session.prefix("audio") + ",\"track\":" + quote(selected.name())
                                    + ",\"rate\":" + std::to_string(format.rate)
                                    + ",\"channels\":" + std::to_string(format.channels)
                                    + ",\"frames\":" + std::to_string(count)
                                    + ",\"timestamp\":" + number(timestamp + static_cast<double>(offset) / format.rate)
                                    + ",\"sequence\":" + std::to_string(sequence++) + '}';
                                session.push(std::move(block));
                            }
                        } catch (...) { input->ReleaseBuffer(frames); throw; }
                        result = input->ReleaseBuffer(frames);
                        if (FAILED(result)) break;
                    }
                    result = input->GetNextPacketSize(&pending);
                    if (FAILED(result)) break;
                }
                return result;
            };
            while (!session.stop && !reopen) {
                if (session.pauseEpoch != epoch || running == session.paused.load()) {
                    unsigned currentEpoch = session.pauseEpoch;
                    bool pause = session.paused;
                    if (running) {
                        check(client->Stop(), "Pause capture stream");
                        check(drain(true), "Preserve paused capture tail");
                        check(client->Reset(), "Reset paused capture stream");
                        running = false;
                    }
                    session.gap(selected.track, session.now(), pause ? "paused" : "resumed");
                    lastEnd = session.now();
                    idleGap = false;
                    if (!pause) { check(client->Start(), "Resume capture stream"); running = true; }
                    epoch = currentEpoch;
                }
                if (selected.followsDefault() && Clock::now() >= nextDefaultCheck) {
                    if (defaultId(devices.Get(), selected.flow(), selected.role()) != id) {
                        reopen = true;
                        break;
                    }
                    nextDefaultCheck = Clock::now() + std::chrono::milliseconds(500);
                }
                if (running) {
                    failure = drain(false);
                    if (FAILED(failure)) break;
                    // A silent loopback engine may deliver no packets at all. Keep
                    // elapsed time explicit without inventing native clocked PCM.
                    if (!idleGap && session.now() - lastEnd > 0.5) {
                        session.gap(selected.track, lastEnd, "no_native_packets");
                        idleGap = true;
                    }
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
            if (FAILED(failure)) {
                if (selected.followsDefault() && failure == AUDCLNT_E_DEVICE_INVALIDATED) {
                    auto replacement = defaultId(devices.Get(), selected.flow(), selected.role());
                    if (!replacement.empty() && replacement != id) { reopen = true; continue; }
                }
                check(failure, "Read selected source (recording on other sources continues)");
            }
            if (!reopen) break;
        }
    } catch (const std::exception& error) {
        session.error(selected.track, error.what());
    }
    if (--session.alive == 0) session.stop = true;
    session.changed.notify_all();
}

int wmain(int argc, wchar_t** argv) {
    try {
        bool selfTest = false, list = false, record = false;
        std::wstring sources = L"both", microphone = L"default:multimedia", system = L"default:multimedia";
        int64_t generation = 0;
        for (int i = 1; i < argc; ++i) {
            std::wstring option(argv[i]);
            if (option == L"--self-test") selfTest = true;
            else if (option == L"--list") list = true;
            else if (option == L"--capture") record = true;
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
        session.event("stopped", "");
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
