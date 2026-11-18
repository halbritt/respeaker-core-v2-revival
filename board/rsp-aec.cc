// rsp-aec - remove the board's own speaker from the microphone channels.
//
//   arecord ... | rsp-aec [--engine aec3|speex] [--linear] [--gentle N] [--tail N] | odaslive
//
// stdin and stdout: raw capture of the ReSpeaker Core v2, 8 channels S32LE at 16 kHz:
// microphones 1-6, then two channels that loop back what the speaker plays. The loopback
// channels pass through unchanged. The microphones come out delayed by 4 ms, always, so
// that the echo is clearly later than the loopback; AEC3 cannot model the board's own
// speaker otherwise, it sits too close.
//
//   aec3    WebRTC AEC3, one instance for all six microphones: an adaptive filter per
//           microphone and one common suppression gain, which keeps the differences
//           between the microphones that the direction finder needs. Default.
//   --linear      aec3: only the adaptive filters, without the suppressor
//   --gentle N    aec3: let N times more echo through before suppressing. 1 is the library's
//                 own tuning, made for a talker louder than the echo. Default 16: measured on
//                 the board, the speaker's voice still never reached a transcript, and more
//                 of a person talking over it survived. At 64 the speaker's voice came through.
//   --tail N      aec3: adaptive filter length in blocks of 4 ms (default 13)
//   speex   libspeexdsp, one adaptive filter per microphone, no suppressor
//
// While the loopback is idle nothing is computed: the microphones are passed on as they
// came, only delayed. The idle loopback is not silent but a few steps of noise.
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>
#include <unistd.h>

#include <api/audio/echo_canceller3_config.h>
#include <api/audio/echo_control.h>
#include <modules/audio_processing/aec3/echo_canceller3.h>
#include <modules/audio_processing/include/audio_processing.h>

extern "C" {
void *speex_echo_state_init(int frame_size, int filter_length);
void speex_echo_cancellation(void *st, const int16_t *rec, const int16_t *play, int16_t *out);
int speex_echo_ctl(void *st, int request, void *ptr);
}

namespace {

const int CH = 8, MICS = 6, RATE = 16000, FRAME = 160;
const int DELAY = 64;          // samples the microphones are held back
const int QUIET = 12;          // loopback sum >> 13 at or below this is idle noise (measured: up to 3)
const int HOLD = 30;           // frames (300 ms) the canceller stays on after the speaker stops

// The stock library does not pass export_linear_aec_output on to AEC3 and then crashes.
class Factory : public webrtc::EchoControlFactory {
  public:
    explicit Factory(const webrtc::EchoCanceller3Config &c) : cfg_(c) {}
    std::unique_ptr<webrtc::EchoControl> Create(int rate, int render, int capture) override {
        return std::make_unique<webrtc::EchoCanceller3>(cfg_, rate, render, capture);
    }

  private:
    webrtc::EchoCanceller3Config cfg_;
};

bool read_all(void *buf, size_t n) {
    char *p = static_cast<char *>(buf);
    while (n) {
        ssize_t r = read(0, p, n);
        if (r <= 0) return false;
        p += r;
        n -= r;
    }
    return true;
}

bool write_all(const void *buf, size_t n) {
    const char *p = static_cast<const char *>(buf);
    while (n) {
        ssize_t r = write(1, p, n);
        if (r <= 0) return false;
        p += r;
        n -= r;
    }
    return true;
}

int32_t to_s32(double x) { return (int32_t)std::max(-2147483648.0, std::min(2147483647.0, std::round(x))); }
int16_t to_s16(long long x) { return (int16_t)std::max(-32768LL, std::min(32767LL, x)); }

}  // namespace

int main(int argc, char **argv) {
    std::string engine = "aec3";
    bool linear = false;
    double gentle = 16;
    int tail = 0;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--engine" && i + 1 < argc) engine = argv[++i];
        else if (a == "--linear") linear = true;
        else if (a == "--gentle" && i + 1 < argc) gentle = atof(argv[++i]);
        else if (a == "--tail" && i + 1 < argc) tail = atoi(argv[++i]);
        else {
            fprintf(stderr, "usage: rsp-aec [--engine aec3|speex] [--linear] [--gentle N] [--tail N]  (8 ch S32LE 16 kHz, stdin to stdout)\n");
            return 2;
        }
    }
    if (engine != "aec3" && engine != "speex") {
        fprintf(stderr, "rsp-aec: unknown engine %s\n", engine.c_str());
        return 2;
    }

    rtc::scoped_refptr<webrtc::AudioProcessing> apm;
    std::vector<void *> speex;
    webrtc::StreamConfig mono(RATE, 1), multi(RATE, MICS);
    if (engine == "aec3") {
        webrtc::EchoCanceller3Config ec;
        ec.filter.export_linear_aec_output = linear;
        if (tail > 0) ec.filter.refined.length_blocks = ec.filter.refined_initial.length_blocks = tail;
        for (auto *t : {&ec.suppressor.normal_tuning, &ec.suppressor.nearend_tuning}) {
            t->mask_lf.enr_transparent *= gentle;
            t->mask_lf.enr_suppress *= gentle;
            t->mask_hf.enr_transparent *= gentle;
            t->mask_hf.enr_suppress *= gentle;
        }
        webrtc::AudioProcessing::Config cfg;
        cfg.echo_canceller.enabled = true;
        cfg.echo_canceller.mobile_mode = false;
        cfg.echo_canceller.export_linear_aec_output = linear;
        cfg.pipeline.multi_channel_capture = true;
        cfg.high_pass_filter.enabled = false;
        cfg.noise_suppression.enabled = false;
        cfg.gain_controller1.enabled = false;
        cfg.gain_controller2.enabled = false;
        webrtc::AudioProcessingBuilder b;
        b.SetEchoControlFactory(std::make_unique<Factory>(ec));
        apm = b.Create();
        apm->ApplyConfig(cfg);
        apm->Initialize();
    } else {
        for (int c = 0; c < MICS; c++) {
            void *st = speex_echo_state_init(FRAME, 1600);
            int rate = RATE;
            speex_echo_ctl(st, 24, &rate);          // SPEEX_ECHO_SET_SAMPLING_RATE
            speex.push_back(st);
        }
    }

    std::vector<int32_t> in(FRAME * CH), out(FRAME * CH);
    std::vector<std::vector<int32_t>> line(MICS, std::vector<int32_t>(DELAY));   // what is held back
    std::vector<int32_t> mic(FRAME);
    std::vector<std::vector<float>> fm(MICS, std::vector<float>(FRAME)), fo(MICS, std::vector<float>(FRAME));
    std::vector<float> fr(FRAME), fro(FRAME);
    std::vector<std::array<float, FRAME>> lin(MICS);
    const float *src[MICS], *rsrc[1] = {fr.data()};
    float *dst[MICS], *rdst[1] = {fro.data()};
    for (int c = 0; c < MICS; c++) {
        src[c] = fm[c].data();
        dst[c] = fo[c].data();
    }
    int16_t rec[FRAME], play[FRAME], clean[FRAME];
    int hold = 0;

    while (read_all(in.data(), in.size() * 4)) {
        long long peak = 0;
        for (int i = 0; i < FRAME; i++) {
            long long r = ((long long)in[i * CH + 6] + in[i * CH + 7]) >> 13;
            peak = std::max(peak, std::llabs(r));
            fr[i] = (float)(((double)in[i * CH + 6] + in[i * CH + 7]) / 4294967296.0);
            play[i] = to_s16(r);
        }
        if (peak > QUIET) hold = HOLD;
        else if (hold > 0) hold--;
        bool active = hold > 0;

        for (int c = 0; c < MICS; c++) {
            // the microphone as it was DELAY samples ago
            for (int i = 0; i < FRAME; i++) mic[i] = i < DELAY ? line[c][i] : in[(i - DELAY) * CH + c];
            for (int i = 0; i < DELAY; i++) line[c][i] = in[(FRAME - DELAY + i) * CH + c];
            if (!active) {
                for (int i = 0; i < FRAME; i++) out[i * CH + c] = mic[i];
            } else if (engine == "aec3") {
                for (int i = 0; i < FRAME; i++) fm[c][i] = (float)(mic[i] / 2147483648.0);
            } else {
                for (int i = 0; i < FRAME; i++) rec[i] = (int16_t)(mic[i] >> 16);
                speex_echo_cancellation(speex[c], rec, play, clean);
                // subtract the estimated echo from the full-precision samples
                for (int i = 0; i < FRAME; i++)
                    out[i * CH + c] = to_s32((double)mic[i] - 65536.0 * (rec[i] - clean[i]));
            }
        }
        if (active && engine == "aec3") {
            apm->ProcessReverseStream(rsrc, mono, mono, rdst);
            apm->set_stream_delay_ms(0);
            apm->ProcessStream(src, multi, multi, dst);
            if (linear) {
                apm->GetLinearAecOutput(rtc::ArrayView<std::array<float, FRAME>>(lin.data(), MICS));
                for (int c = 0; c < MICS; c++)
                    for (int i = 0; i < FRAME; i++) out[i * CH + c] = to_s32(lin[c][i] * 65536.0);
            } else {
                for (int c = 0; c < MICS; c++)
                    for (int i = 0; i < FRAME; i++) out[i * CH + c] = to_s32(fo[c][i] * 2147483648.0);
            }
        }
        for (int i = 0; i < FRAME; i++) {
            out[i * CH + 6] = in[i * CH + 6];
            out[i * CH + 7] = in[i * CH + 7];
        }
        if (!write_all(out.data(), out.size() * 4)) return 1;
    }
    return 0;
}
