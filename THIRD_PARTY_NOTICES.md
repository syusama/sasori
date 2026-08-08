# Third-party notices

Except for the material identified below, Sasori source code and bundled
first-party assets are made available under the MIT License in `LICENSE`.
Third-party licenses and notices remain applicable to their respective
material. This is an engineering inventory for the current source candidate,
not legal advice or a clean-room guarantee.

## Included test material: CPython TLS fixture

`tests/test_providers.py` contains `_TLS_CERT` and `_TLS_KEY`, which are reused
by `tests/test_web_fetch_plugin.py`. Their concatenated PEM bytes match
CPython's public `Lib/test/certdata/keycert.pem` at commit
`063d4555c94ef412c731527dbf30193327f2ee82`:

<https://github.com/python/cpython/blob/063d4555c94ef412c731527dbf30193327f2ee82/Lib/test/certdata/keycert.pem>

Modification: the upstream PEM bundle is split into two Python byte-string
constants. The certificate and key bytes are unchanged. It is used only for
localhost TLS tests, is publicly known, is not a secret, and must never be used
as a production credential.

Copyright (c) 2001-2023 Python Software Foundation; All Rights Reserved.
Additional historical notices apply. The complete license text from that fixed
CPython revision is included verbatim in
`licenses/CPYTHON-3.12-LICENSE.txt` (SHA-256
`3b2f81fe21d181c499c59a256c8e1968455d6689d269aa85373bfb6af41da3bf`).

## Build and container components

`setuptools==80.9.0` is a hash-locked MIT-licensed build dependency. It is not a
Sasori runtime dependency and is not copied from the Docker builder into the
runtime layer as a project dependency.

The Docker distribution is separate from the Python wheel. The declared
runtime base is the digest-pinned DaoCloud mirror of `python:3.12-slim` in the
`Dockerfile`. DaoCloud changes transport, not upstream licensing. The resulting
image includes CPython, pip and vendored components, and Debian libraries under
their own licenses. A released image needs a component-level SBOM from the
actual final digest and must retain its system license files; the Python
package's empty `dependencies` list does not mean the container has no
third-party components. The current runtime image does not install Git.

## External interoperability

Sasori can interoperate with OpenAI Responses, Anthropic Messages, a host Git
executable, and administrator-configured MCP stdio servers. Their SDKs,
binaries, service implementations, credentials, and branding assets are not
bundled by the Sasori Python wheel. The provider adapters use wire protocols,
the Git plugin invokes a separately installed executable, and the MCP adapter
does not vendor an MCP SDK.

## Research references and first-party assets

`docs/FOUNDATION.md` records pinned projects studied for architecture, product,
and UI comparison. The current source audit found no corresponding vendored
package, imported frontend library, font, icon pack, image, or upstream
copyright header. Those entries are research citations, not included software
or claims of endorsement. Any future copied or mechanically translated material
must add its exact upstream project, revision, file, copyright, license, and
modification notice here.

The current Workbench HTML, CSS, JavaScript, and abstract mechanical-scorpion
SVG use native browser APIs, system font fallbacks, and repository-local shapes;
the audit found no external UI asset or official Naruto media. Sasori is an
independent open-source software project and is not affiliated with, endorsed
by, or sponsored by Naruto's creators, publishers, studios, or rights holders.
Third-party names and marks remain the property of their respective owners. A
name/trademark review is still required before a large-scale public launch.
