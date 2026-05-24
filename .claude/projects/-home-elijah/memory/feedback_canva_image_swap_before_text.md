---
name: canva-image-swap-before-text
description: "On a template-based Canva design, the IMAGES carry the brand. Text-only swaps produce parody. Swap images FIRST, then text. Skipping this burned a session."
metadata: 
  node_type: memory
  type: feedback
  originSessionId: de36b780-0890-4b00-bda6-2ad92f2dec80
---

On a template-based Canva design built around photography (Field Manual / Pitch Deck / any visual-heavy layout), the IMAGES carry the brand. Text-only swaps produce parody — the operator's "BIOVEGA" headline floating over tactical-gear-soldier stock photos was the same military template with new labels.

**Rule:** Source/upload images FIRST, then write text last.

Workflow:
1. `start-editing-transaction` to inventory editable image fills (`editable: true`, `type: image`)
2. `get-assets` to see thumbnails of what's currently there
3. Find or generate the replacement images (operator's source designs, Z-Image gen, or operator-provided URLs)
4. `update_fill` operations to swap each image BEFORE any `replace_text`
5. After images are right, then `replace_text` for the headings and body
6. Commit only after operator sees page-1 thumbnail and approves

**Why:** I burned a session producing 141 successful text ops that committed to a still-military-themed design. Operator had to point out the images were unchanged. The text now said "BIO-VEGA — Living Sustainably" but readers still saw soldiers in tactical gear holding rifles. Captured 2026-05-23 during BioVega Field Manual (DAHKgK7YGSg) consolidation work.

**How to apply:**
- Whenever a Canva design has both image fills and text, always do images first
- Before committing a Canva transaction, get the page-1 thumbnail and SHOW the operator visually
- Don't claim "design is now BioVega" just because the text says it — the visual identity comes from the images first

**Related limit (discovered 2026-05-23):**
Canva MCP `get-assets` returns only 133×200 watermarked thumbnails, not original-resolution URLs. So `upload-asset-from-url` with the served URL would degrade asset quality permanently. For now, this means image swaps in DAHKgK7YGSg need to use either:
- Asset IDs from existing Canva designs (operator's hand-painted illustrations live in 11 source designs — see [[elijah-asset-index]] for the catalog)
- Newly-generated images (Z-Image on Hugging Face)
- Operator-uploaded images via the Canva web UI

The DAHKgK7YGSg Phase 2 rebuild (still pending as of 2026-05-23) needs to use `update_fill` with the asset IDs from the source designs to install the canonical hand-painted tent cover (`MAG9lRTBCVU` recommended) and the section diagrams. See [[biovega-real-identity]] Appendix B for the full asset map.
