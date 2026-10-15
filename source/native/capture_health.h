// Clock-injected policy shared by WASAPI capture and non-recording fault tests.
#pragma once
#include <algorithm>
#include <cstdint>

namespace capture_health {
constexpr double kStallSeconds = 3.0;
constexpr double kStableSeconds = 30.0;
constexpr float kActivePeak = 0.00001f;

struct Watchdog {
    double lastPacket = 0;
    double emptySince = -1;
    double activeSince = -1;
    unsigned emptyReads = 0;
    unsigned clockReads = 0;
    unsigned activeReads = 0;
    uint64_t packets = 0;
    uint64_t frames = 0;

    void reset(double now) {
        lastPacket = now;
        emptySince = activeSince = -1;
        emptyReads = clockReads = activeReads = 0;
    }
    void packet(double now, unsigned count) {
        lastPacket = now;
        emptySince = activeSince = -1;
        emptyReads = activeReads = 0;
        ++packets;
        frames += count;
    }
    void emptyAdvertised(double now) {
        if (emptySince < 0) emptySince = now;
        ++emptyReads;
    }
    void noPending() {
        emptySince = -1;
        emptyReads = 0;
    }
    const char* inspect(double now, bool loopback, bool clockValid,
                        bool meterValid, float peak) {
        if (clockValid) ++clockReads;
        else clockReads = 0;
        if (meterValid && peak > kActivePeak) {
            if (activeSince < 0) activeSince = now;
            ++activeReads;
        } else {
            activeSince = -1;
            activeReads = 0;
        }
        if (now - lastPacket < kStallSeconds) return nullptr;
        // An isolated BUFFER_EMPTY race is normal. Persistent advertised
        // packets that cannot be read are independent starvation evidence.
        if (emptySince >= 0 && now - emptySince >= kStallSeconds && emptyReads >= 3)
            return "capture_interruption_zero_frames";
        if (!loopback && clockValid && clockReads >= 3)
            return "capture_interruption_input_starvation";
        // Quiet loopback may legitimately provide no packets indefinitely.
        // Require both an accessible stream clock and sustained endpoint
        // activity before treating missing loopback packets as a failure.
        if (clockValid && clockReads >= 3 && activeSince >= 0
                && now - activeSince >= 1.0 && activeReads >= 3)
            return "capture_interruption_render_starvation";
        return nullptr;
    }
};

struct RecoveryBudget {
    unsigned attempts = 0;
    double stableSince = -1;
    double lastPacket = -1;
    double stableAudio = 0;

    int nextDelay() {
        // ceiling: six retries between thirty seconds of healthy packets.
        // Revisit only with measured reconnect latency; never retry forever.
        if (attempts >= 6) return -1;
        int delay = 250 << std::min(attempts++, 4u);
        stableSince = lastPacket = -1;
        stableAudio = 0;
        return delay;
    }
    void packet(double now, unsigned frames, unsigned rate) {
        if (stableSince < 0 || lastPacket < 0 || now - lastPacket > 0.5) {
            stableSince = now;
            stableAudio = 0;
        }
        lastPacket = now;
        stableAudio += static_cast<double>(frames) / rate;
        if (now - stableSince >= kStableSeconds && stableAudio >= kStableSeconds)
            attempts = 0;
    }
};
} // namespace capture_health
