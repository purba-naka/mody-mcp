# IMPROVEMENT_PLAN.md — Rencana Perbaikan (Phase 6)

**Status: usulan. Belum ada kode yang diubah. Menunggu persetujuan sebelum Phase 7.**

Prinsip: tambahan kecil, backward compatible, tanpa rename/hapus tool, tanpa abstraksi baru.

## Prioritas 0 — Verifikasi cepat (tanpa ubah kode produk)

| # | Tindakan | Alasan |
|---|---|---|
| V1 | Cek mengapa `adk_web.log` memanggil URL Cloud Run tanpa `/mcp` (env shell menimpa `.env`? URL salah?) dan konfirmasi `LLM_API_BASE=.../v1pa` | F3, F15; kemungkinan penyebab langsung "tool hilang" |
| V2 | Dump `list_tools` (server sendiri + Earthdata) dan baca skema yang sampai ke LLM | menutup T1, T4 |
| V3 | Konfirmasi `max_llm_calls` ADK terpasang | H2 |

## Prioritas 1 — Dampak tertinggi, risiko rendah

| # | Perubahan | Berkas | Temuan | Dampak |
|---|---|---|---|---|
| 1 | Ketahanan agent bila toolset down: `McpToolset` dengan error eksplisit / instruksi fallback "jika tool Databricks tidak tersedia, katakan dan jangan pakai tool lain untuk menjawab"; log ERROR | `agent.py` | F3, F8 | Hilangkan jawaban menyesatkan saat server mati |
| 2 | Tool discovery: `list_experiments(prefix?)`, `list_jobs()`, `list_models(catalog, schema)` (read-only, dibatasi allowlist yang sama) | `server.py` | F4 | Hilangkan tebak nama; kurangi call buang |
| 3 | `Filter.op` -> `Literal[...]`; sederhanakan `value`; pesan hint di hasil 0 baris ("0 baris. Cek nilai kolom dengan preview_table; tipe sebenarnya: ...") | `server.py:159`, `run_query` | F2 | Kurangi retry salah tipe |
| 4 | `describe_table`: sertakan contoh nilai/distinct untuk kolom string kardinalitas rendah (opsional `include_samples`) | `server.py:143` | F2 | Agent tahu `padi` vs `2` dari awal |
| 5 | Tandai truncation: `"100 baris (dipotong MAX_ROWS=100)"` bila batas tercapai; beri tahu saat `limit` di-clamp | `run_query`, `clamp` | F7 | Hentikan salah tafsir sampel |

## Prioritas 2

| # | Perubahan | Temuan |
|---|---|---|
| 6 | `aggregate_table(full_name, group_by[], metrics[{column, fn: Literal[count,avg,min,max,sum]}], filters, limit)` — templated, parameter marker, validasi kolom sama seperti `build_query` | F1 |
| 7 | Perjelas deskripsi tanpa rename: awalan domain di `get_run`/`get_job_run`, tambah "Jangan pakai untuk ..." di `preview_table`, `list_tables`, `get_run`; standar docstring: *Apa. Kapan dipakai. Kapan bukan. Input penting. Output.* | F5 |
| 8 | Tambah `annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=...)` dan `title` pada tiap tool; tool tulis `readOnlyHint=False, destructiveHint=False`; `tool_filter` memakai anotasi, bukan daftar hard-coded | F11 |
| 9 | Instruksi agent diperluas (singkat): urutan kerja discovery->describe->query, jangan `include_columns` kecuali perlu, paralelkan hanya call independen, jangan simpulkan statistik dari <30 baris, kondisional soal tool tulis | F8, F12, F6 |
| 10 | Batasi konteks: turunkan `MAX_FILE_CHARS` default (mis. 20k) dengan parameter `offset`/`max_chars`; pangkas hasil tool lama via callback ADK | F6 |
| 11 | Earthdata: pisahkan sebagai agent/sub-agent atau pakai `tool_filter` allowlist nama tool yang dipakai; prefiks via wrapper | F9 |

## Prioritas 3 — Observabilitas & evaluasi

| # | Perubahan | Temuan |
|---|---|---|
| 12 | Perluas audit log: `request_id`/`mcp-session`, `arg_hash` (hash, bukan nilai, menjaga PII), `result_chars`, `rows`, `error_msg` singkat | F10 |
| 13 | Callback ADK `before_tool_callback`/`after_tool_callback`: hitung call per giliran, deteksi (tool, args) identik >=3, hentikan dengan pesan; setel `max_llm_calls` | H2 |
| 14 | Eval set ADK (`eval_sets`) berisi 10 skenario dari `TEST_RESULTS.md` T3 dengan tool stub; jalankan di CI | F5, H1 |
| 15 | Test server baru: skema JSON `list_tools` snapshot (mencegah regresi skema), tes hint 0 baris, tes truncation, tes `aggregate_table` | semua |

## Urutan implementasi Phase 7 (usulan, bertahap)

1. V1-V3 (verifikasi). 2. Item 1, 3, 5 (agent + kecil di server). 3. Item 2, 4. 4. Item 6. 5. Item 7-9. 6. Item 10-15.
Setiap langkah: tes unit baru + `uv run pytest`; tidak ada perubahan yang mengubah signature tool yang sudah ada (hanya menambah parameter opsional / tool baru).

## Risiko & non-tujuan

- Tidak menambah SQL bebas (menjaga model keamanan saat ini).
- Tidak menggabung/menghapus tool tanpa data bukti salah pilih.
- Dampak yang diklaim adalah perkiraan; belum diukur. Ukur ulang dengan eval set (item 14) sebelum/sesudah.
