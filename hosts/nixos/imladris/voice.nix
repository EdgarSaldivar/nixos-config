# Speech for Assist: Whisper in, Piper out.
#
# ⛔ WHY THIS LIVES ON THE PI AND NOT ON THE 4090.
#
# Nardol has a GPU that would transcribe this in milliseconds, and putting STT
# there would still be the wrong choice. Nardol SLEEPS — it suspends to S3 after
# ~15 idle minutes, and a voice command to a sleeping host has to wake it first.
# The pipeline is: wake word -> STT -> LLM -> TTS. With STT on nardol, nothing
# can even be transcribed until the host has finished resuming, so the whole
# 15-25s wake lands in front of the user. With STT here, on a machine that is
# always awake, the Pi transcribes WHILE nardol is waking and most of the resume
# hides behind speech the user was going to spend anyway.
#
# So this is a latency-hiding decision, not a capacity one. Measured on the host
# 2026-09-13: load average 0.06, 6.0 GiB of 7.9 free, 48 C with Samba the only
# real consumer at 2.3% CPU. base.en needs about 1 GB and a couple of cores for
# one to two seconds per utterance. There is ample room.
{ ... }:
{
  services.wyoming.faster-whisper.servers.assist = {
    enable = true;

    # ⚠️ base.en, NOT base. The English-only weights are more accurate than the
    # multilingual ones at identical size, and this house speaks one language.
    # Do not reach for `small` — measured elsewhere at 0.4-0.6x real time on a
    # Pi 5, i.e. slower than the speech it is transcribing. tiny.en is the
    # fallback if base ever proves too slow; it is faster and noticeably worse
    # at proper nouns, which is exactly what this workload is made of.
    model = "base.en";
    language = "en";
    device = "cpu";

    # ⛔ MUST BE PINNED, NOT "auto", OR initialPrompt BELOW IS REJECTED.
    # The module asserts: "Initial prompt is only supported when using
    # `faster-whisper` as `sttLibrary`". Leaving the default silently forfeits
    # the one accuracy lever this host has.
    sttLibrary = "faster-whisper";

    # Greedy decoding. Beam search buys a little accuracy for a lot of latency
    # on a four-core ARM part, and the accuracy that matters here comes from
    # initialPrompt instead.
    beamSize = 1;

    # ⛔ THIS IS THE ACCURACY FIX, AND IT IS FREE.
    #
    # Small Whisper models mishear proper nouns, which is the entire failure
    # mode for a voice assistant: "bedside lamp R" becomes "bedside lamper",
    # and the command silently targets nothing. Seeding the decoder with the
    # actual entity names biases it toward hearing them. The Hailo NPU crowd
    # hit this same wall and concluded the accelerator was not the problem —
    # the missing piece was vocabulary biasing. It costs nothing and needs no
    # hardware.
    #
    # ⚠️ KEEP THIS IN SYNC WITH THE HOUSE. New lights that are not named here
    # will be transcribed worse than the ones that are.
    #
    # ⛔ SINGLE-LETTER NAMES ARE HOMOPHONE TRAPS AND NO PROMPT FIXES THEM.
    # Measured 2026-09-13 by round-tripping Piper speech back through this
    # model: "bedside lamp R on?" transcribes as "bedside lamp ARE on?", and
    # "bedside lamp L on?" as "bedside lamp ALONE?". The letter is only lost
    # when followed by "on" — "turn off bedside lamp R" survives — so it is a
    # collision, not weak recognition, and biasing cannot outvote a real word.
    # "left"/"right" transcribe correctly every time, so those forms lead here
    # and are registered as HA aliases on the entities.
    initialPrompt = builtins.concatStringsSep " " [
      "Home Assistant voice commands."
      "Devices: Bedroom Floor Lamp, Left Bedside Lamp, Right Bedside Lamp,"
      "Bedside Lamp L, Bedside Lamp R,"
      "Ceiling Bulb 1, Ceiling Bulb 2, Ceiling Bulb 3, Ceiling Bulb 4."
      "Commands: turn on, turn off, dim, brightness, warm, cool, percent, scene."
    ];

    # ⛔ int8 AT RUNTIME, NOT A DIFFERENT MODEL NAME.
    # ctranslate2 logs on this host: "compute type inferred from the saved model
    # is float16, but the target device or backend do not support efficient
    # float16 computation ... converted to use the float32 compute type". ARM
    # has no efficient fp16, so the default silently runs the widest, slowest
    # path. int8 is the one CTranslate2 actually optimises on non-Apple aarch64.
    #
    # ⚠️ DO NOT "fix" this by setting model = "base.en-int8". The v3.1.0
    # shorthand parser only recognises tiny/base/small/medium-int8 and maps them
    # to different repositories — there is no English-only int8 shorthand, so
    # that spelling would silently swap base.en for the multilingual base and
    # lose the accuracy the .en weights were chosen for. Selecting the compute
    # type at runtime keeps the model and the initialPrompt intact.
    # Measured head to head on 2026-09-14, same ten commands round-tripped
    # through Piper, scoring whether the ENTITY NAME survived — a misheard lamp
    # name targets nothing, which matters more than word error rate:
    #
    #   int8      entity name survived 10/10   stt median 2.21s
    #   float32   entity name survived 10/10   stt median 3.08s
    #
    # 28% faster for no measurable accuracy cost, so the earlier "accuracy is
    # unverified" caveat is now discharged for clean speech. It says nothing
    # about a noisy room or distance from the mic; if proper nouns start failing
    # in real use, this flag is still the first thing to try reverting.
    extraArgs = [
      "--compute-type"
      "int8"
    ];

    uri = "tcp://0.0.0.0:10300";
  };

  services.wyoming.piper.servers.assist = {
    enable = true;
    # Piper is small enough that the Pi renders a spoken sentence in well under
    # a second; the medium voice is the quality/speed knee. There is no GPU on
    # this host and none is wanted.
    voice = "en_US-lessac-medium";
    useCUDA = false;
    uri = "tcp://0.0.0.0:10200";
  };

  # ⛔ SCOPED TO INTERFACES, NEVER GLOBALLY — the same rule Samba and Jellyfin
  # follow here. Wyoming has NO AUTHENTICATION AT ALL: anything that can reach
  # the port can submit audio and receive transcripts. tailscale0 is required
  # because Home Assistant lives on pelargir and resolves this host over
  # Tailscale, not mDNS — `imladris.local` does not resolve there.
  networking.firewall.interfaces.lan0.allowedTCPPorts = [
    10300 # Wyoming faster-whisper
    10200 # Wyoming piper
  ];
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [
    10300
    10200
  ];
}
