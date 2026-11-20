# WORKFLOW_ANALYSIS.md — Siklus Hidup Tool Calling

Diturunkan dari kode (`agent.py`, `server.py`) dan **jejak nyata** di `session.db`, `adk_web.log`, `mcp.log`.

## 1. Lifecycle

| # | Tahap | Implementasi | Catatan/temuan |
|---|---|---|---|
| 1 | User input | ADK web `/run_sse` | - |
| 2 | Discovery | `McpToolset.get_tools` tiap run (`ListToolsRequest` di `mcp.log`) | Gagal -> hanya WARNING, agent lanjut tanpa tool (**F3**, log 09:38) |
| 3 | Filtering | `tool_filter` lambda (`agent.py:~29`) | Sembunyikan 4 tool tulis; hard-coded set |
| 4 | Seleksi tool | LLM dari nama+docstring+skema semua tool (Databricks 12 + GitHub 4-8 + Earthdata N) | Tidak ada routing/kategori |
| 5 | Argumen | LLM -> JSON, divalidasi pydantic FastMCP | Domain nilai tidak diketahui (**F2**) |
| 6 | Klien | `StreamableHTTPConnectionParams`, Bearer | stateless: sesi baru per call |
| 7 | Server | `AuditedFastMCP.call_tool` (`:46`) -> fungsi tool -> Databricks SDK / GitHub REST | audit log tanpa id korelasi (**F10**) |
| 8 | Hasil | string markdown -> `{content, structuredContent.result}` | Tidak ada penanda truncation (**F7**) |
| 9 | Kembali ke LLM | ADK menambah function_response ke sesi | Tanpa kompaksi (**F6**) |
| 10 | Final | LLM merangkum, bahasa pengguna | Baik; mengakui keterbatasan |

## 2. Pola nyata dari 7 sesi (97 event)

| Sesi | Input | Urutan tool teramati | Pengamatan |
|---|---|---|---|
| 00ea7fca | "halo" | (tidak ada) | Benar tidak memanggil tool; tapi toolset Databricks gagal dimuat saat itu |
| 9dbc0e88 (a) | "halo" | (tidak ada) | Benar |
| 9dbc0e88 (b) | analisa error jagung | list_catalogs -> list_schemas -> list_tables(include_columns) -> [search_runs ‖ query_table] -> [get_run ‖ query_table] -> query_table ‖ 2 -> query_table -> query_table | Urutan logis, paralel pada call independen. 11 call |
| 9dbc0e88 (c) | analisa b12/ndvi padi | query_table ‖ 2 -> query_table -> query_table | Langsung, tidak mengulang discovery (konteks dipakai) |
| 9dbc0e88 (d,e,f) | tanya kode/pipeline | github_list_files, github_read_file (banyak, paralel hingga 6) | Pilihan tool benar; konteks besar |
| 00c4eb19 | analisa b12/ndvi padi | list_catalogs -> list_schemas -> list_tables ‖ 2 -> query_table ‖ 2 (0 baris) -> preview_table -> query_table ‖ 2 -> query_table ‖ 3 | Salah argumen (class=2 vs `padi`), self-correct setelah 2 call |
| 8bc6b7bb | MLflow forecast demand | list_catalogs ‖ github_list_files -> github_list_files ‖ list_schemas -> search_runs (**ditolak**) ‖ list_tables -> preview_table ‖ 2 | Menebak path experiment; menjelajah tanpa arah; akhirnya jujur "tidak menemukan" |
| b51932f0 | pertanyaan MLflow sama | get_collections ‖ 2 (Earthdata) | Tool Databricks tidak termuat; memanggil tool tidak relevan; menjawab jujur |
| e5963837 | analisa jagung | list_catalogs -> "Invocation was aborted by client" | Dibatalkan klien |
| 7a42e423 | 3 dataset GPM | get_collections | Benar, 1 call |

### Yang **tidak** ditemukan dalam data
- Loop berulang dengan argumen identik.
- Pemanggilan tool tak relevan untuk pertanyaan yang tool-nya tersedia (kecuali saat toolset gagal, sesi b51932f0).
- Salah pilih antara `get_run` dan `get_job_run`, atau `preview_table` vs `query_table` (tidak terbukti merugikan).
- Argumen invalid yang lolos validasi server.

### Yang ditemukan
1. Retry akibat label salah tipe (F2).
2. Penjelajahan sia-sia akibat tidak ada discovery experiment (F4).
3. Degradasi diam-diam saat toolset down (F3).
4. Kesimpulan statistik dari sampel kecil (F1).
5. Konteks besar dari pembacaan file berulang (F6).

## 3. Eksekusi paralel & dependensi

Paralel dipakai model dengan benar untuk call independen (mis. 3 `query_table`). Server stateless sehingga aman dipanggil bersamaan (`mcp.log`: sesi berbeda, selesai 813–1343 ms). Dependensi data (run_id, task_run_id, job_id) didokumentasikan di docstring tool dan dipatuhi.

## 4. Penanganan error

| Skenario | Perilaku teramati |
|---|---|
| Experiment di luar prefix | Pesan jelas; agent membaca tapi tidak punya jalur penemuan (F4) |
| 0 baris | Agent menyimpulkan sendiri bahwa tipe/nilai salah, lalu `preview_table` |
| MCP server 404 | Hanya WARNING; agent lanjut (F3) |
| Abort klien | Respons `{"error": "Invocation was aborted by client."}` |

## 5. Pembagian tanggung jawab sumber masalah

Protokol MCP: tidak bermasalah. Definisi/kapabilitas tool: F1, F2, F4, F5, F7. Konfigurasi agent: F3, F9, F8. Konteks: F6. Observabilitas: F10.
