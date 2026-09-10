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

   A parameter can drive **several** nodes by giving a list of targets instead
   of one:

   ```json
   "seed": [["3", "seed"], ["11", "seed"]]
   ```

   That's how `sdxl_hires` keeps its two sampler passes on the same seed, cfg
   and sampler while letting `steps` apply only to the first pass — the refine
   pass keeps its own short step count and low denoise, which is the whole
   point of it.

Parameters the orchestrator can send: `checkpoint`, `prompt`,
`negative_prompt`, `width`, `height`, `batch`, `seed`, `steps`, `cfg`,
`sampler`, `scheduler`, and — for templates that set
`"accepts_init_image": true` — `init_image` and `denoise`.

`init_image` is special: the orchestrator sends image *bytes*, and the worker
uploads them to ComfyUI's input folder first, then substitutes the returned
*filename* into whatever node your map points at (a `LoadImage`). So map it to
the `image` input of your LoadImage node and the plumbing is handled.

Shipped templates:

- **`sdxl_txt2img`** — single pass. Fast, and the default.
- **`sdxl_img2img`** — starts from an uploaded image rather than noise.
  Declares `"accepts_init_image": true`, which is how the orchestrator knows
  it can take one and how the worker rejects a source image sent to a
  workflow that can't use it. `width`/`height` are deliberately unmapped —
  the output size comes from the source image via `VAEEncode`.
- **`sdxl_hires`** — renders at the requested size, upscales the latent 1.5x,
  then runs a short low-denoise second pass. Better fine detail, roughly twice
  the time, and it's the one that actually stretches a 16GB card (the second
  pass at 1536² is where the memory goes).

No worker rebuild is needed to *try* a new graph — the orchestrator can send a
raw graph in `ImageRequestMsg.graph`, which bypasses templates entirely. Adding
it here is how you make it permanent and selectable in the UI.
