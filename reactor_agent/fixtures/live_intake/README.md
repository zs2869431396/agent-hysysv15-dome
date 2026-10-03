These response-only JSON fixtures were extracted from checkpoint-D/live-tr:

- toluene.json: transcript.jsonl
- smr.json: transcript-smr2.jsonl
- gasification_mixture.json: transcript-gas.jsonl
- gasification_inverted.json: transcript-gas2.jsonl
- gasification_missing_water_qwen.json: checkpoint-E/live-qwen-network-20261004/
  transcript-gas-no-input.jsonl (qwen3.7-flash, 2026-10-04)

They preserve the model's facts, including its mistakes. They contain no request
headers or credentials. All five must pass the structural contract. The gasification
fixtures must still pause for composition confirmation: a mixture name is unreadable,
Carbon=38%, Water=62% reverses the stated slurry concentration, and a single coal
entry at 62% must not be normalized into a pure-carbon feed. After explicitly
accepting the source-derived composition default, the compiled mass fractions must
be Carbon=0.62, Water=0.38. These tests never call a model or HYSYS.
