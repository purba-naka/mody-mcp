# TOOL_INVENTORY.md — Inventaris Tool (Phase 2)

Sumber: `server.py` (dibaca langsung). 20 tool pada server mody-mcp. Semua tool mengembalikan **string markdown** (tidak ada `outputSchema`, tidak ada `annotations`). Error = exception -> `Error executing tool <nama>: <pesan>`.

## A. Databricks (12 tool, semua read-only)

| Tool (baris) | Input (wajib*) | Tanggung jawab | Guardrail | Warehouse |
|---|---|---|---|---|
| `list_catalogs` (112) | - | Catalog yang diizinkan | `ALLOWED_CATALOGS` | tidak |
| `list_schemas` (121) | `catalog`* | Schema + komentar | check_catalog | tidak |
| `list_tables` (129) | `catalog`*, `schema`*, `include_columns=False` | Tabel (+kolom) | check_catalog | tidak |
| `describe_table` (143) | `full_name`* | Tipe, format, owner, kolom | split_full_name | tidak |
| `preview_table` (247) | `full_name`*, `limit=20` | Contoh baris | clamp ke `MAX_ROWS` | ya |
| `query_table` (253) | `full_name`*, `columns`, `filters[{column,op,value}]`, `order_by`, `desc`, `limit=20` | SELECT terfilter | kolom divalidasi vs UC, parameter marker, op whitelist, IN<=100 | ya |
| `search_runs` (313) | `experiment_path`*, `filter`, `order_by`, `metrics`, `limit=10` | Daftar run MLflow | prefix experiment | tidak |
| `get_run` (354) | `run_id`* (str) | Params/metrics/tags satu run | cek experiment run | tidak |
| `list_model_versions` (374) | `full_name`*, `limit=20` | Versi model UC + alias | check_catalog | tidak |
| `list_job_runs` (437) | `job_name`*, `limit=10` | Run terakhir job | prefix `mody_` | tidak |
| `get_job_run` (454) | `run_id`* (int) | Status run + task | job allowlist | tidak |
| `get_task_output` (486) | `task_run_id`* (int) | Error + trace (3000 char terakhir) | job allowlist | tidak |

## B. GitHub (8 tool, satu repo)

| Tool (baris) | Input | Sifat | Catatan |
|---|---|---|---|
| `github_list_files` (615) | `path=""`, `ref=""` | baca | kolom `editable` |
| `github_read_file` (633) | `path`*, `ref=""` | baca | potong 60.000 char |
| `github_create_branch` (653) | `branch`* | **tulis** | prefix `agent/` |
| `github_commit_files` (665) | `branch`*, `message`*, `files[{path,content}]`* | **tulis** | <=20 file, <=500k char, allowlist path |
| `github_create_pr` (717) | `branch`*, `title`*, `body`* | **tulis** | selalu draft |
| `github_update_pr` (727) | `number`*, `title`, `body` | **tulis** | hanya PR agent |
| `github_pr_status` (738) | `number`* | baca | check CI + job_id |
| `github_ci_log` (755) | `job_id`* | baca | 3000 char terakhir |

Tool tulis (4) disembunyikan oleh `tool_filter` agent bila `MCP_READ_ONLY=1` (default) — daftar nama hard-coded di `agent.py:8`.

## C. Server pihak ketiga (di luar repo)

`https://cmr.earthdata.nasa.gov/mcp/v1`: hanya `get_collections` teramati di session.db. Jumlah/skema tool lain **tidak diverifikasi**.

## D. Evaluasi per tool (ringkas)

Skala: OK / Perlu perbaikan.

| Aspek | Penilaian | Bukti |
|---|---|---|
| Nama deskriptif | Sebagian | Tanpa prefiks domain di 12 tool Databricks; `get_run` ambigu (MLflow vs Job) |
| Deskripsi informatif | Baik | Docstring ringkas, ada contoh pada `query_table`, `search_runs`; hubungan antar tool disebut (`run_id` -> `get_run`, `job_id` -> `github_ci_log`) |
| "Kapan jangan dipakai" | Hilang | Tidak ada di tool manapun |
| Tumpang tindih | Ada | `preview_table`~`query_table`; `list_tables(include_columns)`~`describe_table`; `get_run` / `get_job_run` |
| Nama parameter | Baik | `full_name`, `catalog`, `schema` konsisten; **tetapi** `run_id` str vs int |
| Tipe & enum | Perlu perbaikan | `Filter.op` str bebas, `value` union lebar; tidak ada `Literal` untuk op; `order_by` MLflow string bebas |
| Default | Baik | `limit` default 10/20, di-clamp (diam-diam) |
| Output konsisten | Cukup | Semua markdown, header jumlah baris; format header berbeda-beda (`N baris`, `Tabel di X (N)`) |
| Error vs sukses | Baik | `isError` + pesan Indonesia actionable (contoh format benar, daftar kolom sah) |
| Verbosity | Variabel | `list_tables(include_columns)`, `github_read_file` 60k char, `get_run` semua params/metrics |
| Granularitas | Cukup | Tidak perlu ditambah banyak; **kekurangan** tool: agregasi, listing experiment/job/model (lihat F1, F4) |

## E. Rekomendasi granularitas (tanpa rename/hapus sembarang)

- **Pertahankan** semua 20 tool (backward compatible).
- **Tambah** (bukan ganti): `aggregate_table`, `list_experiments`, `list_jobs`, `list_models` — total 24, masih wajar.
- **Perjelas** lewat deskripsi, bukan rename: awali `get_run` dengan "MLflow run (bukan Job run)"; `get_job_run` dengan "Databricks Job run".
- **Pertimbangkan** deprecasi lunak `preview_table` (alias ke `query_table`) hanya bila data nyata menunjukkan salah pilih — belum terbukti.
