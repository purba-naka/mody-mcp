# MCP_AUDIT.md — Audit Arsitektur & Implementasi (Phase 1, 3, 5)

Tanggal audit: 2026-11-20. Status: **read-only, tidak ada kode yang diubah.**

## 1. Sumber bukti

| Sumber | Dipakai untuk |
|---|---|
| `mody-mcp/server.py` (812 baris) | Server MCP, 20 tool, middleware audit, auth |
| `adk-mody/mody_agent/agent.py` (39 baris) | Agent Google ADK + LiteLLM + 2 `McpToolset` |
| `adk-mody/mody_agent/.adk/session.db` (7 sesi, 97 event) | **Perilaku agent nyata**: tool call, argumen, respons |
| `adk-mody/adk_web.log`, `mody-mcp/mcp.log` | Error koneksi, audit log server |
| `pytest` (dijalankan) | 95 test lulus |

Tidak bisa diverifikasi: isi lengkap argumen tiap call (audit log server hanya mencatat nama argumen; tapi session.db punya argumen dari sisi agent), perilaku di Cloud Run produksi, daftar lengkap tool server Earthdata (hanya `get_collections` terlihat di sesi), versi model LLM sebenarnya di balik proxy `localhost:20128`.

## 2. Arsitektur (Phase 1)

```
User -> ADK web (adk_web) -> root_agent "mody_agent" (LiteLlm: openai/cc/claude-opus-5-5 via proxy lokal)
   |-- McpToolset #1 -> StreamableHTTP MCP_URL (mody-mcp di Cloud Run, Bearer MCP_API_KEY)
   |        tool_filter: sembunyikan 4 tool tulis GitHub kalau MCP_READ_ONLY=1 (default)
   |-- McpToolset #2 -> https://cmr.earthdata.nasa.gov/mcp/v1 (pihak ketiga, tanpa auth)
```

- **Server** (`server.py`): `FastMCP` disubclass `AuditedFastMCP` (`:43`), `stateless_http=True, json_response=True` (`:58`). Tiap request = sesi baru (log: "Terminating session: None"). Bearer auth ASGI (`:767`), config fail-closed (`:784`).
- **Registrasi tool**: dekorator `@mcp.tool()`; skema dari type hints + docstring. Tidak ada `title`, `annotations` (readOnlyHint dll.), maupun `outputSchema`.
- **Klien**: ADK `McpToolset` — otomatis `list_tools` tiap run, mengubah ke function declaration LLM. Tidak ada kode klien custom.
- **Orkestrasi**: ADK `Agent` standar (loop ReAct bawaan). **Tidak ada** callback, guard duplikat, batas iterasi eksplisit, planner, atau sub-agent.
- **Hasil tool -> LLM**: string markdown dibungkus `{"content":[{type:text}], "structuredContent":{"result":"..."}}` (terlihat di session.db). Teks yang sama dikirim dua kali di event, tapi ke LLM kemungkinan sekali (belum diverifikasi).
- **Error**: exception dilempar -> FastMCP membungkus `Error executing tool X: <pesan>` (isError). Tidak ada retry di server; retry hanya di ADK untuk `get_tools`.
- **State**: stateless di server; riwayat di SQLite ADK. Tidak ada kompaksi/pemangkasan konteks.

## 3. Kesimpulan utama

MCP server **secara protokol sehat** (skema valid, error actionable berbahasa jelas, guardrail kuat, 95 test lulus). Dari 7 sesi nyata, agent memilih tool dengan cukup tepat. Masalah "tool calling berantakan" yang terlihat **bukan terutama bug protokol MCP**, melainkan gabungan: (a) celah kapabilitas/penemuan di tool, (b) skema argumen yang tidak memberi domain nilai, (c) degradasi diam-diam saat toolset gagal dimuat, (d) instruksi agent yang tipis, (e) tidak adanya observabilitas yang bisa mendeteksi pola buruk. Gejala "loop tak berujung" dan "duplicate call" **tidak ditemukan** dalam data (call berulang yang ada berupa paralel dengan argumen berbeda).

## 4. Temuan terurut prioritas (Phase 5)

Legenda: **[C]** = Confirmed (ada bukti di kode/log/sesi), **[H]** = Hipotesis, butuh pengujian.

### P1 — High

**F1 [C] Tidak ada agregasi; agent menyimpulkan dari sampel kecil/bias.** `query_table` hanya SELECT baris (`server.py:253-270`, `build_query:165`), tanpa GROUP BY/COUNT/AVG, tanpa ORDER BY acak. Sesi 00c4eb19 & 9dbc0e88: agent mengambil 5–100 baris lalu menyimpulkan statistik; agent sendiri menulis "belum kuat secara statistik … tanpa agregasi". Dampak: 3 call per kelas hanya untuk 5 baris sampel, kesimpulan lemah. Severity High. Solusi: tool agregat terstruktur (`aggregate_table`: group_by + metric enum count/avg/min/max/sum), tetap templated (tanpa SQL bebas). Dampak: call turun, kualitas jawaban naik.

**F2 [C] Skema tidak memberi domain nilai -> argumen salah.** `Filter.op: str` (`:161`) bukan `Literal`, sehingga JSON schema tanpa enum; `value` berupa union lebar (`:162`) -> `anyOf` rumit yang sering di-flatten/ditolak oleh konverter Gemini/LiteLLM. Semua nilai dicast ke STRING (`:204`, komentar `:220`). Sesi 00c4eb19: agent query `pred_class = 2` -> 0 baris (x2, paralel), lalu `preview_table`, lalu ulang: 2 call sia-sia + 1 putaran. `describe_table` tidak menampilkan contoh nilai/distinct value kolom kategorikal. Solusi: `op: Literal[...]`, tambah `distinct_values`/contoh nilai di `describe_table`, atau hint "0 baris" yang menyarankan `preview_table`. Severity High (bukan crash, tapi buang call).

**F3 [C] Toolset gagal dimuat -> agent lanjut tanpa tool dan diam-diam.** `adk_web.log` 2026-11-20 09:38: `POST ...run.app` 404 -> `Failed to get tools from toolset McpToolset` (hanya WARNING di `llm_agent.py:187`); agent tetap jalan. Sesi b51932f0 membuktikan dampaknya: pertanyaan MLflow dijawab dengan memanggil `get_collections` (Earthdata, tidak relevan) lalu agent bilang "tidak ada tool Databricks". Instruksi agent (`agent.py:21-24`) menyuruh "selalu panggil tool" tanpa fallback saat tool tidak ada. Severity High. Catatan [H]: URL di log tidak berisi `/mcp` padahal `.env` `MCP_URL` memuat `/mcp` -> kemungkinan env shell menimpa `.env` atau URL Cloud Run salah; belum bisa diverifikasi.

**F4 [C] Celah penemuan (discovery) -> agent menebak nama.** Tidak ada tool untuk daftar experiment MLflow, nama job, atau model UC; padahal `search_runs(experiment_path)`, `list_job_runs(job_name)`, `list_model_versions(full_name)` mewajibkannya. Sesi 8bc6b7bb: agent menebak `/Workspace/Shared/mody/demand_forecast` -> ditolak (`check_experiment :305`), lalu menjelajah `list_catalogs`, `github_list_files`, 2x `preview_table` tanpa hasil. Sesi 9dbc0e88 berhasil hanya karena nama sudah ada di konteks. Severity High.

### P2 — Medium

**F5 [C] Nama dan deskripsi ambigu antar tool serupa.** `get_run` (MLflow, `run_id: str`, `:354`) vs `get_job_run` (Jobs, `run_id: int`, `:454`) vs `search_runs` vs `list_job_runs`: kata "run" dipakai untuk dua domain, parameter `run_id` bertipe beda. Tool Databricks tanpa prefiks, GitHub berprefiks `github_` (tidak konsisten). `preview_table` vs `query_table` tumpang tindih (preview = query tanpa argumen). `list_tables(include_columns=True)` vs `describe_table` tumpang tindih. Deskripsi hampir tidak ada "kapan TIDAK dipakai". Dalam sesi nyata belum terlihat salah pilih akibat ini (belum terbukti kerugian), jadi Medium. Hipotesis H1: dengan model lebih lemah dari opus-5.5 risiko naik.

**F6 [C] Konteks membengkak.** `github_read_file` hingga 60.000 karakter/file (`MAX_FILE_CHARS :512`); sesi 9dbc0e88 membaca 6 file sekaligus dalam satu giliran, berulang 3 giliran. `list_tables(include_columns=True)` dipakai untuk schema kosong. Tidak ada kompaksi sesi. Medium.

**F7 [C] Output tidak menandai pemotongan.** `run_query` mengembalikan `"100 baris"` (`:236`) saat sama dengan `MAX_ROWS`; agent tidak bisa membedakan "tepat 100" dari "dipotong". `clamp` (`:287`) diam-diam mengecilkan `limit`. Tanpa `ORDER BY`, LIMIT tidak deterministik. Medium.

**F8 [C] Instruksi agent tipis.** `agent.py:20-24`: satu paragraf; tidak ada urutan kerja (discovery -> describe -> query), aturan efisiensi (jangan `include_columns` bila tidak perlu, paralelkan hanya call independen), aturan bila tool tidak tersedia, atau konvensi label. "Selalu panggil tool" bertentangan dengan sapaan (untungnya model tidak memanggil tool untuk "halo" — sesi 00ea7fca/9dbc0e88).

**F9 [C] Server Earthdata pihak ketiga digabung ke agent yang sama** (`agent.py:31-37`, komentar "ponytail: testing only"). Menambah tool tak terkontrol (nama/skema tak diaudit; hanya `get_collections` teramati), tanpa auth, tanpa prefiks -> risiko tabrakan nama dan pemanggilan di luar topik (terjadi di sesi b51932f0). Medium.

**F10 [C] Observabilitas tidak cukup untuk mendiagnosis kualitas tool calling.** `AuditedFastMCP` (`:43-55`) mencatat nama tool, nama argumen, outcome, ms — tanpa correlation/session id, ukuran hasil, jumlah baris, hash argumen. Tidak bisa mendeteksi duplikat atau loop dari sisi server (stateless, tiap call sesi baru). Satu `ToolError` di `mcp.log:54` (search_runs, 0 ms) tidak bisa dijelaskan penyebabnya dari log. Medium.

### P3 — Low

- **F11 [C]** Tidak ada `annotations` MCP (`readOnlyHint`, `idempotentHint`, `openWorldHint`) dan `title`; klien/UI tidak bisa membedakan baca vs tulis kecuali lewat daftar hard-coded `WRITE_TOOLS` (`agent.py:8`) yang bisa drift dari server.
- **F12 [C]** Instruksi menyebut "branch agent/*" sementara 4 tool tulis disembunyikan (`MCP_READ_ONLY=1`) -> agent bisa menjanjikan commit yang tidak mampu dilakukan. Perlu kalimat kondisional.
- **F13 [C]** Output berupa string markdown bukan data terstruktur; ada `structuredContent:{"result": "<string>"}` yang hanya membungkus string yang sama. Tidak merusak, tapi parsing nilai (mis. confusion matrix JSON dalam sel, terpotong 200 karakter oleh `cell :88`) rawan.
- **F14 [C]** `cell()` memotong tiap sel ke 200 karakter (`MAX_CELL`), termasuk kolom JSON (`proba_json`, `confusion_matrix`) -> data diam-diam terpotong tanpa penanda per-sel kecuali `...`.
- **F15 [H]** `LLM_API_BASE=http://localhost:20128/v1pa` — kemungkinan typo (`/v1`). Sesi tetap berhasil memanggil LLM, jadi mungkin proxy menerima; perlu konfirmasi pemilik. Tidak mempengaruhi tool calling secara langsung.
- **F16 [C]** File `.env` berisi token (Databricks, GitHub, LLM) ada di folder proyek yang bukan repo git; pastikan tidak pernah di-commit/dibagikan. (Tidak ada nilai yang disalin ke laporan.)
- **F17 [C]** Sesi e5963837: `"Invocation was aborted by client"` pada call pertama — dibatalkan pengguna/klien, bukan bug server.

## 5. Sumber masalah per kategori (Phase 3)

| Kategori | Terbukti? | Temuan |
|---|---|---|
| MCP protokol/implementasi | Tidak ada bug | skema valid, 95 test hijau; kekurangan hanya annotations/outputSchema (F11,F13) |
| Definisi tool | Ya | F2, F5, F7, F14 |
| Kapabilitas tool | Ya | F1, F4 |
| Konfigurasi agent framework | Ya | F3, F9, tanpa batas/guard (lihat bawah) |
| Prompt engineering | Ya | F8, F12 |
| Orkestrasi | Sebagian | paralel OK; tidak ada guard duplikat/iterasi (H2) |
| Konteks | Ya | F6 |
| Perilaku LLM | Sebagian | tidak berhenti mencari saat tool tidak ada; menyimpulkan dari sampel kecil |

Aspek orkestrasi (Phase 3) yang diperiksa:
1. Strategi pilihan tool: murni LLM dari deskripsi; semua ~20+ tool selalu terekspos.
2. Loop: ADK default; **batas iterasi tidak diset** di `agent.py` (default ADK `RunConfig.max_llm_calls` tidak diverifikasi di versi terpasang — H2). Tidak ada loop nyata di 97 event; giliran terpanjang = 4 round tool (sesi 9dbc0e88).
3. Paralel vs sekuensial: **paralel berjalan benar** (mis. 3 `query_table` bersamaan, `mcp.log` 01:24:50-52). Dependensi output->input dipatuhi (list_catalogs -> list_schemas -> list_tables -> query_table; get_job_run -> get_task_output terdokumentasi di docstring).
4. Pemahaman hasil: baik; agent mengoreksi diri setelah 0 baris ("Class mungkin string nama. Cek isi.").
5. Penentuan selesai: baik; agent menutup dengan ringkasan dan mengakui keterbatasan.
6. Duplicate prevention: tidak ada mekanisme; tidak terbukti perlu di data.
7. Retry/error: `ToolError` dibaca agent dan ditangani (sesi 8bc6b7bb) tetapi lalu "mengembara" (F4).
