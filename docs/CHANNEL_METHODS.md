
## ⚠️ GỠ KÊNH KHỎI DASHBOARD — BẪY "GÁN TRẦN" (26/8/2026)

Gỡ 55 kênh thế hệ 1 khỏi `RS_PRESETS`/`RS_BRANDS`/`brands.json` xong thì **toàn bộ tab dashboard
đơ, không bấm được gì**. Lỗi console:

    TypeError: Cannot set properties of undefined (setting 'cat')   @ index.html:3832

Vì ngoài ba nơi khai báo, còn **60 câu gán trần** rải rác trong thân script:

    RS_BRANDS.DATARACE.cat="27";  RS_BRANDS.BALDBANDIT.cat="23";  …   (55 câu)
    RS_BRANDS.TRUETALES.art={avatar:"…",cover:"…"};                   (5 câu)

Gỡ khoá đi thì câu ĐẦU TIÊN ném lỗi, và vì nó nằm giữa thân script nên **mọi dòng sau đó không
chạy** — kể cả dòng 5213 là chỗ gắn `onclick` cho thanh điều hướng. Lỗi hiện ở dòng 3832 nhưng
triệu chứng là "tab không bấm được", chẳng liên quan gì nhau.

**LUẬT:** không gán trần `OBJ.KHOA.thuoc_tinh = …` thành dãy dài. Dùng vòng lặp CÓ KIỂM:

    for (const k in MAP) { if (RS_BRANDS[k]) RS_BRANDS[k].cat = MAP[k]; }

**LUẬT 2:** gỡ một khoá khỏi bảng dùng chung thì phải quét **mọi phép gán thuộc tính** vào bảng đó,
không chỉ nơi khai báo. Quét bằng `OBJ\.[A-Z0-9]+\.[a-z_]+\s*=`, và quét theo THUỘC TÍNH BẤT KỲ —
lần này em tìm mỗi `.cat` nên sót `.art`, phải sửa hai vòng.

**LUẬT 3:** nghiệm thu bằng trình duyệt thật: mở trang, đọc console, rồi bấm/đi qua TỪNG tab. Đo
`typeof nav.onclick === "function"` cho cả 15 mục — file nạp được không có nghĩa là script chạy hết.
