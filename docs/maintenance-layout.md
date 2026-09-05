# Maintenance Layout

## Completed splits

| Previous hotspot | Previous size | Current responsibility split |
|---|---:|---|
| `web/index.html` | about 6,300 lines | semantic HTML, one stylesheet, and four ordered behavior scripts |
| `tests/test_protocol_adapters.py` | about 9,700 lines | discoverable aggregate, shared fixture, and five feature-oriented case groups |

The browser assets remain plain HTML/CSS/JavaScript and require no bundler. The server exposes an explicit asset allowlist rather than mapping `/assets/` to a directory. The test aggregate checks duplicate method names so a future mixin cannot silently shadow an existing regression.

## Remaining backend hotspot

`glm2api.py` is still the largest source file at roughly 15,000 lines. Its current sections share mutable runtime state, persistence locks, request accounting, cleanup executors, and test monkeypatch points. Moving those sections verbatim into files would replace a visible monolith with circular imports and hidden cross-module globals, so backend extraction should follow state isolation rather than line-count slicing.

Recommended extraction order:

1. Introduce an explicit runtime-services object for logging, metrics, stores, cleanup queues, and captcha workers.
2. Move protocol-neutral request normalization, tool codecs, and response builders into `glm2api_protocol.py` with no network or storage imports.
3. Move Z.ai HTTP transport, uploads, SSE parsing, retry, and task/chat control into `glm2api_upstream.py`.
4. Split management, OpenAI, Responses, Anthropic, and panel routes into handler mixins that receive runtime services explicitly.
5. Leave `glm2api.py` as composition root, CLI entry point, and compatibility re-export layer until downstream imports have migrated.

Each extraction should preserve the public symbols used by clients, run all protocol tests from a dependency-free clone, and avoid changing wire payloads in the same commit as a file move.

## Verification invariants

- Concatenating `core.js`, `history.js`, `chat.js`, and `admin.js` in document order must preserve the previous application program.
- `styles.css` must preserve the previous inline stylesheet.
- The split test groups must contain exactly the same test method names as the aggregate they replaced.
- `/assets/` must serve only allowlisted files with `nosniff` and CSP headers; traversal-like paths must return 404.
- `python -m unittest discover`, `npm test`, and the public release scan must pass from a clean candidate tree.
