# Mini App do'kon вЂ” sozlash

## Bu nima?

Do'kon endi bot xabarlari o'rniga chiroyli veb-sahifa (Telegram Mini App)
ko'rinishida ham ochilishi mumkin вЂ” foydalanuvchi asosiy menyudagi
**"вњЁ Mini-App do'kon"** tugmasini bosadi, Telegram ichida sahifa ochiladi,
mahsulot yoki boxni tanlaydi va **haqiqiy Telegram Stars** bilan
to'g'ridan-to'g'ri shu yerdan to'laydi.

Alohida hosting kerak emas вЂ” sahifa botning o'zi ishlatadigan serverda
(`bot.py` ichidagi aiohttp) `/webapp` manzilida beriladi. To'lov Telegram'ning
o'z Stars invoys mexanizmi orqali ishlaydi, muvaffaqiyatli to'lov esa xuddi
oddiy bot-chat orqali xarid qilingandagi kabi qayta ishlanadi (admin xabar
oladi, mahsulot yetkazib beriladi) вЂ” kodning ikkala qismi bir xil funksiyani
ishlatadi.

## Sozlash kerakmi?

**Ha, bitta narsa: `PUBLIC_BASE_URL`.** Telegram Mini App tugmasi faqat
**HTTPS** manzil bilan ishlaydi.

- Agar `WEBHOOK_URL` allaqachon sozlangan bo'lsa (webhook rejimi) вЂ” hech
  narsa qilish shart emas, bot avtomatik o'shani ishlatadi.
- Agar **polling** rejimida ishlatsangiz (Render'da `WEBHOOK_URL` bo'sh,
  lekin xizmatning o'zi baribir `https://sizning-nom.onrender.com` manzilida
  turadi) вЂ” `PUBLIC_BASE_URL` ni shu manzilga qo'lda o'rnating:

  **Render dashboard в†’ xizmatingiz в†’ Environment:**
  ```
  PUBLIC_BASE_URL=https://referalbot.onrender.com
  ```

Sozlanmasa вЂ” hammasi avvalgidek ishlayveradi, faqat "вњЁ Mini-App do'kon"
tugmasi asosiy menyuda ko'rinmaydi (chunki Telegram HTTP manzilni qabul
qilmaydi).

## Do'konga narsa qanday chiqadi?

Mini App'da avtomatik ko'rinadi:
- **Gift / Premium** toifasidagi, `в­ђ narxi` (price_stars) belgilangan
  do'kon mahsulotlari.
- **`рџ’« Telegram Stars narxi`** o'rnatilgan boxlar (Admin panel в†’ рџ“¦ Boxlar
  boshqaruvi в†’ box tanlang в†’ "рџ’« Narx (TG Stars)").

`в­ђ Yulduz` (star) toifasi va NFT bo'limi Mini App'da ko'rinmaydi вЂ” ular
alohida mexanikaga ega (UZS to'lov / guruhga yo'naltirish), Stars bilan
to'g'ridan-to'g'ri sotib olinadigan narsalar emas.

## Texnik tafsilot (qiziqqan uchun)

- `/webapp` вЂ” Mini App'ning o'zi (bitta HTML fayl, Telegram WebApp JS SDK
  bilan).
- `/api/shop` вЂ” GET, sotib olinadigan mahsulot/box ro'yxatini JSON qilib
  qaytaradi.
- `/api/create_invoice` вЂ” POST, tanlangan mahsulot/box uchun Telegram Stars
  invoys havolasini yaratadi (`bot.create_invoice_link`). Mini App bu
  havolani `Telegram.WebApp.openInvoice()` orqali ochadi.
- To'lov muvaffaqiyatli o'tgach, Telegram bot'ga oddiy `successful_payment`
  yangilanishini yuboradi вЂ” bu allaqachon mavjud bo'lgan
  `successful_payment_handler` orqali qayta ishlanadi (o'zgarish yo'q).
