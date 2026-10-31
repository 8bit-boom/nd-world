# Hybrid or Rust-only? One page

Two images are published from the same code (`main`):

- **Hybrid**: `ghcr.io/8bit-boom/nd-world:latest`. The Rust gateway answers what it can; everything else is handed to the Python app inside the same container.
- **Rust-only**: `ghcr.io/8bit-boom/nd-world-gateway:latest` (`ND_PYTHON_FREE=1`). No Python. A request the gateway cannot answer gets "Not available here" (a page) or a generic 400 (a save).

## Side by side

| | Hybrid | Rust-only |
|---|---|---|
| Image size | about 1.1 GB | about 746 MB |
| Memory at rest | about 346 MiB | about 26 MiB |
| A request the gateway does not answer | Python answers it | refused (501 / generic 400) |
| Odd or unusual stored data | Python's code reads it, as it always did | each reader must handle it in Rust, or the page is refused |
| Features Python-only today | none missing | character `.pdf` export, world Foundry export, FastAPI `/docs`, legacy Ollama routes, a few tag-list and King-in-Yellow calls |
| Rolling back | n/a | change the image line back |
| Moving between them | one line in the compose file; same data folder, same format | |
| Needs patching / updating | Python and its libraries too | the gateway only |

## What the tests say, and what they do not

- Every ported route is compared with Python's answer in the test suite (about 6,500 tests, run through both servers). Where the gateway answers, it answers like Python.
- About 90% of the app's pages work with no Python at all (the crawl of every parameterless page). The long tail of rare input is not covered the same way.
- The bugs found in real use of Rust-only (October 2026) were all things the tests never produced: AI Chat history (open / save / delete), the AI status dot, the sheet roller, starring a picture, the model list, AI rewrite, an entity with a rich-text note, a world with an accented name, and entity pages that still fail on real data (cause not yet known). Each worked in the hybrid and failed in Rust-only. Expect more of this kind until real use has had time to find them.

## Which to run

- **Playing at the table**: the hybrid. It has every feature, and a gap in the gateway cannot take a page down.
- **Trying Rust-only**: on a second port with a copy of the data (`world-pf` in the compose file), `ND_DECLINE_TRACE=1`, a few sessions. Every refusal is printed as a `DECLINE` line in the container log.
- **Switch when**: the log stays quiet for a few weeks of real use, and `scripts/python_free_entity_crawl.py` on a copy of your data reports every entity page 200.
- **What Rust-only buys**: about 300 MiB of memory and 350 MB of disk, no Python to maintain. It matters on a small machine; on a big NAS it is a nicety.

## How to test Rust-only on your own data

`docs/PYTHON_FREE_LOCAL_TEST.md` (restore a backup into a copy, run the entity crawl, read the `DECLINE` lines).
