# Development

## Layout

- `__init__.py`: the LichtFeld entry point (`on_load`, `on_unload`).
- `panels/`: the retained RML panel, its styles and the logo. The logo is an uncompressed 32-bit TGA.
- `cyango_lfs/`: export, upload and state. It does not import `lichtfeld`, so the tests run without the host.
- `tests/`: unit tests with a fake Cyango API and fake S3 (`tests/fake_cyango.py`).

The plugin uses the Cyango External Publish API v1. The contract is in `cyango-context/repo-contexts/cyango-backend/external-publish-api.md`.

## Local setup

LichtFeld Studio needs an NVIDIA GPU with CUDA, on Windows or Linux. Link your checkout into the LichtFeld plugin folder. The destination must not exist.

Windows (PowerShell, from the repository root):

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.lichtfeld\plugins" | Out-Null
New-Item -ItemType Junction -Path "$HOME\.lichtfeld\plugins\cyango" -Target (Resolve-Path .)
```

Linux (from the repository root):

```bash
mkdir -p ~/.lichtfeld/plugins
ln -s "$(pwd)" ~/.lichtfeld/plugins/cyango
```

In the LichtFeld Python console:

```python
import lichtfeld as lf

lf.plugins.discover()
lf.plugins.settings("cyango").set("load_on_startup", True)
lf.plugins.load("cyango")
lf.plugins.start_watcher()
```

With `hot_reload = true`, a saved Python file reloads the plugin. The watcher does not watch `.rml` and `.rcss` files. After you change them, touch a Python file or run `lf.plugins.reload("cyango")`. If a reload fails, read `lf.plugins.get_error("cyango")`.

## Use another Cyango environment

| Setting | Effect |
|---------|--------|
| `base_url` plugin setting | The API base URL, for example `https://<api-host>/external-publish/v1`. It wins over the variable. |
| `CYANGO_API_URL` | The same, as an environment variable. |
| `CYANGO_CACHE_DIR` | Where staged exports and upload manifests go. |

Plain `http` is accepted only for `localhost` and `127.0.0.1`. The plugin checks HTTPS certificates. For a local backend with a self-signed certificate, point `SSL_CERT_FILE` at a file that holds that certificate before you start LichtFeld.

```python
lf.plugins.settings("cyango").set("base_url", "https://localhost:<port>/external-publish/v1")
```

## Tests

From the repository root, with Python 3.10 or later:

```console
python -m unittest discover -s tests -v
ruff check . && ruff format --check .
```

The tests never call a real Cyango server. Before a release, test in LichtFeld Studio:

1. Connect with a real token. The account and the folders show.
2. Publish a small scene (single PUT) and a scene larger than 64 MiB (multipart).
3. Cancel during the upload, then resume. Only the missing parts upload.
4. Open the story link. The splat shows after the conversion.
