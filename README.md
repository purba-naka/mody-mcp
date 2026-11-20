# mody-mcp

MCP server read-only untuk Unity Catalog, MLflow, dan Jobs Databricks, lewat Streamable HTTP di `/mcp`.
Spesifikasi lengkap ada di `docs/2026-10-30-mody-mcp-generik-design.md`.

| Tool | Isi | Warehouse |
|---|---|---|
| `list_tables(catalog?, schema?, include_columns=False)` | Daftar tabel. `catalog` kosong = catalog tunggal yang diizinkan, `schema` kosong = semua schema | tidak |
| `describe_table(full_name, include_samples=False)` | Kolom, tipe, komentar; opsional nilai contoh kolom teks | opsional |
| `query_table(full_name, columns, filters, order_by, desc, limit, sample)` | Baris mentah; tanpa filter = contoh baris, `sample=True` untuk sampel acak | ya |
| `aggregate_table(full_name, metrics, group_by, filters, order_by, desc, limit, join)` | Agregat (count/count_distinct/avg/min/max/sum/stddev/median) per kelompok. `join` menggabung tabel kedua (kolom diawali `b.`) dalam catalog yang diizinkan; kolom array diringkas dengan `array`=mean/max/min atau `at`=n | ya |
| `list_models(catalog?, schema?, full_name?, metrics, limit)` | Tanpa `full_name`: daftar model UC. Dengan `full_name`: versi, alias (`champion`), opsional metrik run | tidak |
| `search_runs(experiment_path?, filter, order_by, metrics, limit, run_id?)` | Run MLflow, tag git, metrik. `experiment_path` kosong = semua experiment yang diizinkan. `limit=1` atau `run_id` = sertakan params/metrics/tags lengkap | tidak |
| `job_runs(job_name?, run_id?, limit)` | Tanpa argumen: daftar job. `job_name`: run terakhir + detail run terbaru. `run_id`: detail satu run. Detail selalu menyertakan error dan trace task yang gagal | tidak |

Tool MLflow dibatasi `ALLOWED_EXPERIMENT_PREFIX`, tool job dibatasi `ALLOWED_JOB_PREFIX`.

### GitHub (satu repo, `GITHUB_REPO`)

| Tool | Isi |
|---|---|
| `github_read(path, ref)` | Path folder = daftar isi dengan kolom `editable`, path file = isi file teks |
| `github_commit_files(branch, message, files, pr_title?, pr_body?)` | Satu commit berisi beberapa file ke branch `agent/*` (branch dibuat kalau belum ada). `pr_title` diisi = sekaligus buka draft PR ke `GITHUB_BASE_BRANCH` |
| `github_update_pr(number, title, body)` | Ubah PR milik agent |
| `github_pr_status(number)` | State PR, check CI, dan bagian akhir log check yang gagal |

Pembatas tulis: branch harus `GITHUB_BRANCH_PREFIX`, path harus cocok `GITHUB_ALLOWED_PATHS` dengan kedalaman folder
yang sama, `.github/` dan `conftest.py`/`__init__.py`/`setup.py`/`pyproject.toml` selalu ditolak, maksimal 20 file
atau 500.000 karakter per commit, update ref tanpa force, PR dari fork ditolak, PR selalu draft.
`github_pr_status` hanya untuk PR dari branch agent. Merge tetap oleh manusia (tidak ada tool merge).

Server menolak start kalau `MCP_API_KEY` kurang dari 32 karakter, atau `ALLOWED_CATALOGS`,
`ALLOWED_EXPERIMENT_PREFIX`, `ALLOWED_JOB_PREFIX`, `GITHUB_BRANCH_PREFIX` kosong.
`HOST` default `127.0.0.1`. Untuk akses remote, set `HOST` dan pasang reverse proxy TLS di depannya.

## Menjalankan

1. Isi env. Lihat `.env.example` untuk daftar lengkap.
2. Pasang dependency:
   ```bash
   uv sync
   ```
3. Jalankan server dengan `.env`:
   ```bash
   uv run --env-file .env python server.py
   ```

## Mencoba dengan MCP Inspector

```bash
bunx @modelcontextprotocol/inspector
```

Isi koneksi di Inspector:
- Transport: Streamable HTTP
- URL: `http://localhost:8000/mcp`
- Header: `Authorization: Bearer <MCP_API_KEY>`

## Tes

```bash
uv run pytest
```

Tes tidak butuh koneksi ke Databricks.
