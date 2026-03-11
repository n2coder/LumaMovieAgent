class LumaVadProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._samplesSinceEmit = 0;
    this._sumsq = 0;
    this._peak = 0;
    this._targetSamples = Math.max(1, Math.floor(sampleRate * 0.02)); // ~20ms
  }

  process(inputs) {
    const input = inputs && inputs[0];
    if (!input || !input.length) return true;

    const channels = input.length;
    const frames = input[0].length || 0;
    if (!frames) return true;

    for (let i = 0; i < frames; i += 1) {
      let mixed = 0;
      for (let c = 0; c < channels; c += 1) {
        mixed += input[c][i] || 0;
      }
      mixed /= channels;
      const abs = Math.abs(mixed);
      this._sumsq += mixed * mixed;
      if (abs > this._peak) this._peak = abs;
      this._samplesSinceEmit += 1;
    }

    if (this._samplesSinceEmit >= this._targetSamples) {
      const rms = Math.sqrt(this._sumsq / this._samplesSinceEmit);
      this.port.postMessage({
        type: "vad_frame",
        rms,
        peak: this._peak,
      });
      this._samplesSinceEmit = 0;
      this._sumsq = 0;
      this._peak = 0;
    }

    return true;
  }
}

registerProcessor("luma-vad-processor", LumaVadProcessor);

