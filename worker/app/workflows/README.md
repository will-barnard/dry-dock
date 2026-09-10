# Workflow templates

One JSON file per named workflow. The filename (minus `.json`) is the name the
orchestrator sends and the Darkroom UI shows, and the worker advertises every
template it finds here in its register message.

To add one:

1. Build it in the ComfyUI UI until it renders what you want.
2. Enable dev mode in ComfyUI settings, then **Save (API Format)** — the normal
   save produces a UI graph, which this loader can't drive.
3. Save the exported JSON as the `graph` value in a file here.
4. Write the `map`: `"<parameter>": ["<node id>", "<input name>"]`, for the
   parameters you want the orchestrator to control. Anything you leave out
   keeps whatever the template says, which is the point — a template can carry
   its own opinionated sampler and scheduler and simply not expose them.

Parameters the orchestrator can send: `checkpoint`, `prompt`,
`negative_prompt`, `width`, `height`, `batch`, `seed`, `steps`, `cfg`,
`sampler`, `scheduler`.

No worker rebuild is needed to *try* a new graph — the orchestrator can send a
raw graph in `ImageRequestMsg.graph`, which bypasses templates entirely. Adding
it here is how you make it permanent and selectable in the UI.
