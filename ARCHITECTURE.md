# L3A Architecture Record

Tài liệu mô tả các quyết định thiết kế có thể kiểm chứng của workflow L3A. Không chứa prompt,
chain-of-thought hay API key. Toàn bộ logic là deterministic (không dùng LLM), nên cùng một bộ
evidence luôn cho cùng một output.

## 1. System overview

```text
inputs/<case_id>.json
        │  (cli: case_received)
        ▼
  ┌─────────────┐  task_assigned   ┌──────────────┐  get_order, get_order_items
  │ Coordinator │ ───────────────► │ order-agent  │ ─────────────────────────► MCP
  │ (triage +   │ ◄─────────────── │              │  handoff ORDER_CONTEXT_READY
  │  compose)   │                  └──────────────┘
  │             │  task_assigned   ┌──────────────┐  get_payment_timeline,
  │             │ ──────┬────────► │ payment-agent│  get_refund_timeline ────────► MCP
  │             │       │ (song song)└────────────┘
  │             │       └────────► ┌──────────────┐  get_shipment_summary ──────► MCP
  │             │ ◄─────────────── │shipment-agent│  handoff SHIPMENT_LATE/ON_TIME
  │             │  task_assigned   ┌──────────────┐  get_policy ────────────────► MCP
  │             │ ───────────────► │ policy-agent │  policy_decided + handoff
  │             │  (nếu seller chịu trách nhiệm) order-agent: get_sellers ───────► MCP
  │             │  task_assigned   ┌──────────────┐
  │             │ ───────────────► │   verifier   │  verification_completed PASS/FAIL
  └─────────────┘ ◄─────────────── └──────────────┘
        │  (cli: validate schema → ghi file → case_finalized)
        ▼
outputs/<case_id>.json          traces/trace.jsonl
```

Mã nguồn:

| File | Vai trò |
| --- | --- |
| `src/student_agent/workflow.py` | `solve_case()` + `Coordinator`: điều phối, triage, compose output, fallback |
| `src/student_agent/agents.py` | Specialist agents (order, payment, shipment, policy) và `Verifier` |
| `src/student_agent/a2a.py` | A2A envelope `A2AMessage` + `A2ABus` (mirror vào trace, chống vòng lặp) |
| `src/student_agent/evidence.py` | `EvidenceLedger`: cổng duy nhất tới MCP, phân quyền tool, retry, scope check |
| `src/student_agent/analysis.py` | Hàm thuần: lọc evidence theo timeline, dedupe, triage theo thứ tự ưu tiên |

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case input (`claimed_order_id`, `opened_at`, `claims`, `policy_version`) | Giao task, chạy payment/shipment song song, triage primary issue từ facts, compose output, chọn evidence trích dẫn, fallback khi verifier từ chối | Output JSON cho CLI |
| Order/item (`order-agent`) | `order_id`, `opened_at` | Lấy order row (nguồn authoritative cho timeline), lọc item nằm trong vòng đời order, dedupe dòng trùng; xác minh seller khi được yêu cầu | `ORDER_CONTEXT_READY` / `ORDER_NOT_FOUND`; `SELLER_CONFIRMED` / `SELLER_UNCONFIRMED` |
| Payment (`payment-agent`) | `order_id` + `OrderWindow` | Lọc payment/refund event trong `[purchase, opened_at]`, xác định capture gắn với thời điểm approve, mismatch mở, refund pending/failed | `PAYMENT_FACTS_READY` / `PAYMENT_EVIDENCE_MISSING` |
| Shipment (`shipment-agent`) | `order_id` + `OrderWindow` | So giao hàng với ngày dự kiến, so handoff carrier với shipping limit, chỉ nhận event trùng thời điểm giao thực tế | `SHIPMENT_LATE` / `SHIPMENT_ON_TIME` / `SHIPMENT_NOT_DELIVERED` |
| Policy (`policy-agent`) | `primary_issue`, `policy_version`, seller của order | Tra rule trong policy: `case_status`, `recommended_action`, `refund_brl`, bên chịu trách nhiệm; gán `party_id` seller thật của order (policy chỉ chứa seller mẫu) | `policy_decided` + `POLICY_APPLIED` / `POLICY_UNAVAILABLE` |
| Verifier | Output nháp + ledger + policy decision | Kiểm tra invariant (mục 6) trước khi finalize | `verification_completed` + `VERIFIED` / `REJECTED` |

Phân quyền tool (`TOOL_PERMISSIONS` trong `evidence.py`, vi phạm → `PermissionError`):

| Actor | Tool được gọi |
| --- | --- |
| order-agent | `get_order`, `get_order_items`, `get_sellers` |
| payment-agent | `get_payment_timeline`, `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |
| coordinator, verifier | không gọi tool |

Không dùng `get_order_payments` (dữ liệu là tập con của `get_payment_timeline`),
`get_product_context` (không ảnh hưởng kết luận) và `get_customer_history` (order không
cung cấp `customer_unique_id`, không đoán giá trị này).

## 3. A2A protocol

- Envelope `A2AMessage`: `message_id`, `case_id` (khóa correlation), `sender`, `recipient`,
  `kind` (`task` | `result`), `intent`/decision code, `payload`, `evidence_refs`, `hop`.
- `A2ABus` được tạo mới cho mỗi case và từ chối message có `case_id` khác → không thể trộn
  evidence/facts giữa các case.
- Mỗi `assign()` sinh trace `task_assigned` (actor = người giao, target = người nhận,
  `decision_code` = intent). Mỗi `handoff()` sinh trace `handoff` kèm `evidence_refs` bàn giao
  và `in_reply_to` trỏ về task gốc.
- Điều kiện handoff: specialist chỉ trả kết quả sau khi evidence đã qua validate và scope check;
  seller follow-up chỉ chạy khi policy xác định seller chịu trách nhiệm.
- Chống vòng lặp: luồng là DAG cố định (không có agent nào gửi task ngược), agent không tự gửi cho
  chính mình, và bus có ngân sách `MAX_HOPS_PER_CASE = 24` message/case.
- Timeout: HTTP client của gateway có timeout 300s đọc / 30s connect (starter kit); lỗi transport
  được retry có giới hạn (mục 5).
- Trace chỉ chứa sự kiện quan sát được (decision code, tool, evidence ref, số dòng, hash);
  không chứa suy luận nội bộ.

## 4. Evidence lifecycle

1. **Discovery**: `EvidenceLedger` gọi `list_tools()` một lần mỗi gateway session và từ chối tool
   không được quảng bá — không đoán tên tool.
2. **Call**: luôn truyền đúng `case_id` của case hiện tại; tham số lấy từ input (`claimed_order_id`,
   `policy_version`).
3. **Validate**: `EvidenceGateway.call` validate envelope theo `mcp-evidence-response-v1`. Ledger
   kiểm tra thêm entity scope: mọi `order_id` trong data phải trùng order được hỏi; policy phải đúng
   `policy_version`. Evidence sai scope bị loại (trace `EVIDENCE_REJECTED_OUT_OF_SCOPE`), không cite.
4. **Lưu**: ledger lưu `Evidence(tool, actor, ref, domain, result_hash, data)` theo case; mỗi tool
   chỉ gọi một lần/case (cache). `evidence_ref` được giữ nguyên, không sửa, không tự tạo.
5. **Trace**: mỗi evidence dùng được sinh `tool_result_consumed` với `evidence_refs=[ref]`, domain,
   `result_hash`, số dòng. Tool không tìm thấy (ví dụ order không có refund) sinh
   `tool_result_consumed` với `EVIDENCE_NOT_FOUND`, không có ref.
6. **Lọc nhiễu trong dữ liệu**: gateway trả kèm các dòng thuộc timeline khác (dòng lệch ngày hàng
   tháng, dòng trùng hệt). Order row là nguồn authoritative cho timeline:
   - payment/refund event chỉ nhận trong `[order_purchase_timestamp, opened_at]`;
   - capture "của giao dịch" là capture trong 24h kể từ `order_approved_at`;
   - item chỉ nhận khi `shipping_limit_date` nằm trong `[purchase, estimated_delivery]`;
   - shipment event chỉ nhận khi trùng thời điểm `delivered_customer_at` thật;
   - độ trễ giao hàng tính từ timestamp của order/shipment, không từ event lẻ;
   - các dòng giống hệt nhau được dedupe trước khi phân tích.
7. **Triage** (thứ tự ưu tiên cố định, để dòng nhiễu không lật kết luận): order `canceled`/
   `unavailable` có capture → refund `failed` → refund `pending` → reconciliation mismatch `open`
   → capture bằng nhau vượt tổng order (duplicate) / capture cộng đúng tổng order (split) → giao
   trễ (seller nếu handoff carrier sau shipping limit, ngược lại logistics) → `unsupported_claim`.
8. **Map vào output**: chỉ cite evidence thật sự hỗ trợ kết luận (`CITATIONS` trong
   `workflow.py`), ví dụ `refund_failed` → refund timeline + payment timeline + policy;
   `late_delivery_seller` → order + shipment + item + seller + policy. Claim assessment dùng tập con
   của `evidence_refs` top-level. Không tái sử dụng evidence giữa các case (ledger và bus theo case).

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / lỗi transport | Có, tối đa 3 lần, backoff tuyến tính 0.5s (tool read-only nên idempotent) | Coi như evidence không có | `tool_result_consumed` / `EVIDENCE_NOT_FOUND` |
| Not found (tool trả `isError`) | 1 lần retry (lỗi not-found là deterministic) | Thiếu `get_order`/payment/shipment → `insufficient_evidence`, `needs_investigation`, refund 0, confidence 0.3; thiếu refund timeline → hiểu là không có refund event | `EVIDENCE_NOT_FOUND`; handoff `ORDER_NOT_FOUND` / `PAYMENT_EVIDENCE_MISSING` / `SHIPMENT_EVIDENCE_MISSING` |
| Source conflict (order row ≠ shipment summary; timestamps ≠ event actor) | Không | Chọn nguồn authoritative (order row; handoff timestamps), ghi vào `data_conflicts`, giảm confidence 0.10 | `data_conflicts[].resolution_code` = `PREFER_AUTHORITATIVE_ORDER_ROW` / `PREFER_HANDOFF_TIMESTAMPS` |
| Evidence sai scope (order/policy khác) | Không | Loại evidence, không cite | `EVIDENCE_REJECTED_OUT_OF_SCOPE` |
| Invalid specialist result / verifier từ chối | Không | Compose lại output `insufficient_evidence` an toàn rồi verify lại; nếu vẫn fail thì dừng run (không ghi output sai) | `verification_completed` / `FAIL` rồi `PASS` |

Không bao giờ chuyển evidence thiếu thành dữ liệu phỏng đoán: `payment_references` và
`shipment_ids` để rỗng vì evidence không có định danh payment/shipment.

## 6. Verification invariants

Verifier (`agents.Verifier.check`) chạy trước khi finalize mỗi case:

- **Schema**: output hợp lệ theo `l3a-output-v2.schema.json`.
- **Case/entity scope**: `case_id` khớp; `affected_entities.order_ids == [claimed_order_id]`.
- **Evidence ownership**: mọi `evidence_refs` nằm trong ledger của chính case này (lấy trong run
  hiện tại); nếu policy quyết định thì policy evidence phải được cite.
- **Claim linkage**: có đúng một assessment cho mỗi `claim_id` trong input; evidence của claim là tập
  con của `evidence_refs` top-level.
- **Money totals**: tổng `refund_lines[].amount_brl` = `recommended_refund_brl`, currency BRL.
- **Status/refund/action**: `no_action` và `needs_investigation` ⇒ refund = 0;
  `action_required` ⇒ refund > 0; `no_action` ⇒ action duy nhất `document_no_action`; không có
  action trùng.
- **Seller responsibility**: mọi responsible party loại `seller` phải có `party_id` nằm trong
  `affected_entities.seller_ids`.
- **Confidence bounds**: `0 ≤ confidence ≤ 1`.

Kiểm tra chéo số tiền: coordinator so `refund_brl` của policy với số tiền evidence hỗ trợ (capture
khi approve, refund failed, mismatch, khoản trùng, freight). Lệch → giảm confidence 0.05.

Confidence: 0.95 khi bằng chứng rõ; −0.05 nếu còn tín hiệu cạnh tranh trong phạm vi; −0.10 nếu
claim của khách mâu thuẫn evidence; −0.10 nếu có data conflict; 0.3 cho `insufficient_evidence`.

## 7. Reproducibility

- Model: không dùng LLM; toàn bộ quyết định deterministic từ evidence + policy.
- Python ≥ 3.11 (đã chạy trên 3.14); dependency theo `pyproject.toml` (`mcp>=2,<3`, `httpx2`,
  `jsonschema`, `python-dotenv`). `mcp_gateway.py` đọc `is_error` (mcp 2.x) và fallback `isError`
  (mcp 1.x).
- Concurrency: các case chạy tuần tự; trong một case payment-agent và shipment-agent chạy song song
  (`asyncio.gather`) trên cùng MCP session. Không có random seed.
- Tài nguyên: 1 MCP session cho cả run; 6–8 MCP call/case (780 call cho 100 case, gồm 1 retry khi
  refund timeline not-found và `get_sellers` khi seller chịu trách nhiệm), ~3.5 phút.
- Điều kiện: team phải có run đang mở (đăng nhập workspace `/l3a` bằng Team API Key một lần) trước
  khi gọi tool; nếu chưa có, mọi tool trả `Error executing tool`.
- Lệnh chạy:

```bash
python -m pip install -e ".[dev]"
day09 validate-inputs
day09 run
day09 validate
day09 package --output dist/submission.zip
pytest -q tests/test_workflow.py tests/test_starter.py   # test offline, không cần MCP
```
