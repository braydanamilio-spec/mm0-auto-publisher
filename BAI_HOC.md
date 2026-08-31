# BÀI HỌC MM0 — bản ngắn, dễ đọc

Ghi những cái **đã mắc thật**, mỗi cái một đoạn. Chi tiết kỹ thuật đầy đủ nằm ở
`render-pipeline/PIPELINE_RULES.md`; file này để đọc nhanh trước khi sửa gì.

---

## 24/8/2026 — đêm sửa quota

### 1. Cạn hạn mức rồi vẫn gọi tiếp = tự đốt
**Lượt gọi THẤT BẠI vì 429 vẫn bị trừ vào hạn mức.** Nên mọi cơ chế "thử lại cho chắc" biến thành
máy đốt quota khi đã cạn.

Đã mắc ở **ba** chỗ khác nhau trong cùng một đêm:
| Chỗ | Quên cái gì | Thiệt hại đo được |
|---|---|---|
| `set_ai_pool` | key nào đã cạn Vision/vẽ ảnh — **xoá sổ mỗi video** | 108 lượt gọi vào key đã chết / 1 phiên |
| `_retry` | project nào đã cạn | thử lại 5 lần vô ích, ~22s mỗi lệnh |
| `pool_accounts` | project A đã cạn | ~20.000 lượt đọc hỏng / 1 phiên |

> **Luật:** chỗ nào biết "cái này đã cạn" mà lại quên đi theo chu kỳ ngắn, đều là máy đốt hạn mức.
> Trí nhớ phải sống ít nhất bằng **chu kỳ hạn mức của nhà cung cấp**, không phải bằng vòng đời một video.

### 2. Nghỉ theo PHÚT khác cạn theo NGÀY
Cùng ra mã 429 nhưng chữa ngược nhau:
- **Chặn theo phút (RPM/TPM)** → key vẫn tốt, nghỉ **2 phút**.
- **Cạn theo ngày (RPD/TPD)** → nghỉ tới **mốc reset** (00:00 giờ Thái Bình Dương).
- Không rõ → 20 phút.

Trước đây gộp một mức 90 phút ⇒ một key chỉ dính RPM bị **ném đi 88 phút hạn mức còn dùng được**.
Hồ 153 key mà mỗi cơn RPM lại loại một key 90 phút thì tới trưa còn vài key là đúng.

### 3. Vòng xoáy tự siết
A cạn → không dựng nổi doc gói danh sách kho → mỗi luồng quay về lối cũ (thử A 3 lần × 73 doc, mỗi
30 phút, × 18 luồng) → **A bị đập mạnh hơn** → hôm sau vừa reset là cháy lại.

> **Luật:** đường dự phòng **không được phụ thuộc vào chính thứ nó dự phòng**.

### 4. Hỏng CÂM là loại lỗi tốn nhất
- Gương B→B2 hỏng **16 tiếng** mà log chỉ có đúng một dòng cảnh báo.
- Tính năng clip video chết **100% một phiên** mà log vẫn sạch bong.

Cả hai đều vì: hàm chỉ in khi **thành công**, mọi đường thất bại `return None` không nói gì; và cả
thân hàm nằm trong **một** `try` nên hỏng bước đầu là mất sạch.

> **Luật:** một tính năng không có chỉ số đo được là một tính năng **có thể đã chết mà không ai biết**.
> Nay mỗi khâu có `📈 tỉ lệ dùng được`, khâu nào thử ≥3 lần mà 0 lần được thì hét `🚨 CHẾT CÂM`.

### 5. Không ghim phiên bản = không thể gỡ lỗi
`pip install google-cloud-firestore` không ghim số ⇒ mỗi phiên cài "bản mới nhất hôm đó". Một bản
phát hành mới làm gãy `.stream(timeout=…)` ⇒ gương B→B2 chết âm thầm.

> **Luật:** mã nguồn không đổi mà hành vi đổi theo **ngày cài đặt** thì không thể suy luận ra sự cố.
> Đã có `render-pipeline/constraints.txt` + in phiên bản thư viện vào log mỗi phiên.

### 6. Vòng lặp `for kênh in ...` có truy vấn bên trong = bom hẹn giờ
`auto_enqueue` quét 40 doc × 55 kênh × 48 lượt/ngày ≈ **105.000 lượt đọc** — một mình gấp đôi trần
50K. Mà 39/40 doc lấy về đều đã xử lý từ lâu.

> **Luật:** thấy khuôn đó thì đổi sang **một truy vấn có cờ**. Số kênh tăng thì lượt đọc tăng theo cấp số.

### 7. Chia việc TĨNH khi tải lệch nhau
18 luồng, mỗi luồng nhận **cứng** một kênh. Kênh nhẹ xong sau 20 phút, kênh nặng chạy 1h53 ⇒ 17 máy
ngồi không, phiên kế nằm chờ.

> **Luật:** tải lệch nhau thì phải **hàng chờ + ai xong trước lấy tiếp**. Và mọi phép lấy việc dùng
> chung phải **NGUYÊN TỬ** (giao dịch), nếu không hai máy nhận trùng việc.

### 8. Hai lớp đồ hoạ khai báo toạ độ rời rạc
Lớp số liệu to đè lên lớp phụ đề. Bố cục chọn bằng **băm % 4** nên chỉ **1/4 số video** dính — mắt
người soi vài cái thì không thấy, và QC cũng chấm cao vì chữ vẫn "sạch, đọc rõ".

> **Luật:** phải có **hằng số băng dùng chung** + một bài kiểm chốt bằng số trong `selftest.py`.
> Lỗi chỉ xuất hiện ở 1/4 trường hợp thì không thể phát hiện bằng cách nhìn.

### 9. Thao tác tưởng thất bại — kiểm trước khi làm lại
Xếp hàng render lại bị treo giữa chừng ⇒ tưởng hỏng ⇒ chạy lại 2 lần ⇒ **7 video thành 21 yêu cầu**.
Cờ chống trùng trên bản ghi không cứu được vì dữ liệu trong trình duyệt là **ảnh chụp**, chưa kịp làm tươi.

> **Luật:** thao tác ghi bị treo **không có nghĩa là chưa ghi**. Kiểm ở nguồn trước khi chạy lại, và
> mọi việc chạy hàng loạt phải có **khoá đang-chạy** + sổ đã-làm ngay trong phiên.

### 10. Việc rẻ thì đừng chờ
Từng hoãn việc soi video lỗi vì "sợ tốn quota". Thực tế: dữ liệu **đã nằm sẵn trong bộ nhớ trình
duyệt** — tính ngay trên đó là **0 lượt đọc**. Ra kết quả trong 2 phút thay vì chờ nửa ngày.

> **Luật:** trước khi hoãn vì "tốn quota", tính thử xem tốn bao nhiêu. Phần lớn là rẻ hơn tưởng, và
> có đường 0 đồng nếu chịu nghĩ.

---

## Nguyên tắc chung rút ra

1. **Đo trước, đoán sau.** Mọi kết luận trong ngày đều phải kèm con số lấy từ log.
2. **Hỏng phải ồn ào.** Im lặng là trạng thái nguy hiểm nhất.
3. **Trí nhớ đúng tầm.** Cái gì hết theo ngày thì đừng nhớ theo video.
4. **Dự phòng phải độc lập** với thứ nó dự phòng.
5. **Việc dùng chung phải nguyên tử.** Nhiều máy chạy song song thì không có "chắc là không trùng đâu".

## 31/8 — LOGIC DỌN KHO: dọn xong phải ĐÓNG SỔ, không thì dashboard nói dối

Anh báo "vẫn chưa dọn xoá videos cũ, kho còn 2067". Chạy thử workflow dọn với `scope=store`
trên 100 kho: **thấy 0 file**. Kho đã sạch từ trước; chỉ con số trên màn hình là sai.

### Vì sao con số sai

Worker đọc sổ `kho_that` (bảng D1) rồi trả về cho dashboard, kèm nhãn "✓ kho thật". Sổ ấy do
bộ lập kế hoạch ghi **một lần mỗi ngày**, sau khi đi hết các kho Drive. Worker có kiểm tuổi sổ
— quá 26 giờ thì bỏ, quay về đếm bản ghi — nhưng 26 giờ là quá dài sau một lần dọn lớn:

    ngày N, 02:00   plan đếm kho  -> ghi sổ: 2067
    ngày N, 10:00   dọn sạch kho  -> kho còn 0, SỔ VẪN 2067
    ngày N, 10:05   anh mở dashboard -> "2067 ✓ kho thật"

Nhãn "✓ kho thật" làm chuyện tệ hơn hẳn một con số cũ: nó khẳng định con số ấy vừa được đếm
từ Drive, nên không ai nghĩ tới việc nghi ngờ.

### Luật rút ra

**Mọi thao tác làm THAY ĐỔI kho phải đóng sổ đếm ngay trong cùng thao tác ấy.** Dọn xong mà
không cập nhật sổ thì hệ có hai sự thật, và cái sai lại là cái được hiển thị.

Cụ thể còn thiếu ở workflow dọn: sau khi bỏ file vào thùng rác, phải ghi `kho_that = <số thật
vừa đếm được>` (thường là 0) thay vì để plan hôm sau ghi hộ. Đó là một dòng, và nó xoá hẳn cả
lớp hiểu nhầm này.

### Hai chỗ khác cùng họ, phát hiện trong cùng lượt

**Bản ghi kho thiếu `root`.** Log dọn liệt kê hàng loạt: `DATA_RACE`, `BEYOND`, `BRIGHT_LINE`,
`FIXED_POINT`, `LINE_ITEM`... đều `root=''` nên bị LOẠI HẲN khỏi danh sách quét. Nghĩa là nếu
những kho ấy CÓ file thì lệnh dọn cũng không chạm tới, mà báo cáo vẫn ghi "thành công". Một
bản ghi hỏng không được phép biến thành một kho vô hình — phải đếm riêng và báo lên đầu.

**Firestore B cạn hạn mức ngày (429).** Bộ đếm chạy "success" nhưng không ghi được gì. Kết quả
là chạy lại bao nhiêu lần cũng không sửa được con số, và không có dấu hiệu nào cho biết vì sao.
Một lượt chạy không ghi được kết quả thì KHÔNG PHẢI "thành công".

