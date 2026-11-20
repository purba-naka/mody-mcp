# TEST_RESULTS.md — Hasil Pengujian (Phase 4)

**Prinsip**: hanya hasil yang benar-benar dijalankan/diamati yang dilaporkan sebagai hasil. Tes live agent+LLM+Databricks **tidak dieksekusi** dalam audit ini (butuh LLM proxy lokal, kredensial Databricks/GitHub, dan server yang sedang hidup; tidak ada yang boleh dimodifikasi pada fase ini).

## 1. Dieksekusi: test suite server

Perintah: `uv run pytest -q` di `mody-mcp/` -> **95 passed, 1 warning** (deprecation httpx/starlette testclient). 2.55 s.
Cakupan: validasi guardrail, builder query, auth, format output (tanpa koneksi Databricks, sesuai README). **Tidak** menguji pilihan tool oleh LLM.

## 2. Diamati dari jejak nyata (bukan dieksekusi ulang)

Sumber: `session.db` (argumen & respons), diamati, bukan diulang. Kolom "Status" = apa yang bisa dinyatakan.

| Skenario (dari matriks) | Input nyata | Tool diharapkan | Tool aktual | Argumen aktual / bermasalah | Hasil | Masalah |
|---|---|---|---|---|---|---|
| Single-tool | "cari 3 dataset curah hujan GPM, ringkas" (7a42e423) | get_collections | get_collections | keyword, limit=3, has_granules | Benar | - |
| Unrelated (sapaan) | "halo" (00ea7fca, 9dbc0e88) | tanpa tool | tanpa tool | - | Benar | - |
| Multi-tool / dependen | analisa jagung (9dbc0e88) | catalog->schema->tabel->query | sesuai | output->input benar | Berhasil, 11 call | konteks besar, sampel kecil (F1) |
| Invalid/ambigu argumen | padi class=2 (00c4eb19) | filter `padi` | filter `2` | nilai numerik salah | 0 baris x2 -> koreksi | F2: 3 call terbuang |
| Tool failure | experiment `demand_forecast` (8bc6b7bb) | cari nama yang benar | `search_runs` ditolak | path tebakan | agent berhenti jujur | F4: tidak ada discovery |
| Tool tidak tersedia | pertanyaan MLflow (b51932f0) | tool MLflow / klarifikasi | `get_collections` (tidak relevan) | - | jawaban jujur tanpa data | F3 |
| Repeated request | - | - | - | - | **Tidak teramati** duplikat identik | tidak bisa dinyatakan lulus |
| Missing arguments | - | - | - | - | **Tidak teramati** | tidak bisa dinyatakan |
| Complex workflow | gabungan DB+GitHub (9dbc0e88 d-f) | list/read berurutan | sesuai | paralel 6 `github_read_file` | Berhasil | konteks sangat besar (F6) |
| Abort | e5963837 | - | list_catalogs lalu abort | - | error terkontrol | bukan bug |

## 3. Belum dijalankan (diperlukan untuk menutup hipotesis)

| ID | Tes yang disarankan | Cara | Menutup |
|---|---|---|---|
| T1 | Skema JSON `query_table` yang diterima LLM (apakah `anyOf` di-flatten?) | dump `list_tools` via MCP Inspector / klien python | F2 |
| T2 | `max_llm_calls` default ADK terpasang & perilaku saat tool selalu error | baca versi ADK + tes dengan tool gagal | H2 |
| T3 | Matriks 10 skenario di bawah, dijalankan terhadap agent dengan tool stub/mock (tanpa Databricks) | harness eval ADK (`eval_sets`) | F5, H1 |
| T4 | Ambil `list_tools` dari Earthdata, cek tabrakan nama | skrip klien | F9 |
| T5 | Penyebab URL tanpa `/mcp` di log 09:38 | cek env shell vs `.env`, `curl` ke URL Cloud Run | F3 |
| T6 | Model lebih lemah (mis. tanpa opus) pada set pertanyaan yang sama | ganti `LLM_MODEL` | H1 |

### Matriks skenario untuk T3 (belum dijalankan)

| Tes | Input | Tool diharapkan |
|---|---|---|
| Single | "daftar tabel di mody_dev.core" | list_tables (include_columns=false) |
| Multi | "bandingkan macro_f1 dua run terbaik" | search_runs(order_by) lalu get_run x2 |
| Dependen | "kenapa job mody_train_dev gagal" | list_job_runs -> get_job_run -> get_task_output |
| Ambigu | "lihat run terakhir" | tanya klarifikasi MLflow vs Job |
| Unrelated | "apa itu F1 score?" | tanpa tool |
| Argumen kurang | "ambil data tabel" | tanya nama tabel |
| Argumen invalid | `query_table` kolom tak ada | ValueError dengan daftar kolom -> perbaiki 1x |
| Failure | warehouse down | laporkan error, tidak loop |
| Repeated | pertanyaan yang sama 2x | tidak memanggil ulang bila sudah di konteks |
| Complex | "analisa kesalahan kelas X dan usulkan perubahan kode" | DB lalu GitHub, tanpa tebak nama |
