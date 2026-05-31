# NotNativeMemory Codex Hooks

Codex has its own lifecycle hook contract, so this bundle is Codex-specific.
It shares the same NNM goals as the Claude and NNA bundles, but the adapter
shape is tailored to Codex events and output envelopes.

Installed location:

```text
~/.codex/hooks/notnative-memory/
```

Registered events:

- `UserPromptSubmit` - captures the user prompt, injects relevant memories,
  and uses `verbatim_recent` when the prompt is low-signal.
- `SessionStart` - injects a small working-set context block.
- `PostToolUse` - passively captures tool call/result summaries.
- `Stop` - passively captures the final assistant response or transcript tail.

The bundle is additive and non-blocking. If NNM is unavailable, Codex continues
without memory context.

The installer merges registrations into `~/.codex/hooks.json`. Codex may ask
you to trust the new hooks with `/hooks` before they run.
