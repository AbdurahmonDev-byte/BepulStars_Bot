# Botni VPS'ga ko'chirish (Render'dan chiqib)

Render limit to'lib qolgani uchun botni o'z VPS serveringizga o'tkazish
bo'yicha to'liq qo'llanma. Hammasi Docker orqali ishlaydi — server tozalanib
qolsa yoki ko'chirilsa ham, bir necha buyruq bilan qayta ishga tushadi.

## 0-qadam: nima kerak bo'ladi

- **VPS server** (Hetzner, DigitalOcean, Timeweb, Selectel va h.k.) — Ubuntu
  22.04 yoki 24.04, eng arzon tarif (1 vCPU / 1-2 GB RAM) yetarli.
- **Domen** (ixtiyoriy, lekin tavsiya etiladi) — Mini App va webhook uchun
  HTTPS shart. Domeningiz bo'lmasa, pastdagi "Domensiz variant" qismiga
  qarang — bepul yechim bor.
- Bot allaqachon ishlatayotgan **Turso** ma'lumotlar bazasi (`TURSO_DATABASE_URL`
  va `TURSO_AUTH_TOKEN`) — bular o'zgarmaydi, VPS'da ham xuddi shu
  ma'lumotlar bazasidan foydalanasiz, hech qanday ma'lumot yo'qolmaydi.

## 1-qadam: VPS sotib olish

Har qanday provayderda eng arzon Ubuntu 22.04/24.04 serverini oling:
- **Hetzner Cloud** — CX22 (~€4/oy), eng arzon va ishonchli variantlardan biri.
- **DigitalOcean** — Basic Droplet ($6/oy).
- **Timeweb Cloud** — arzon rubl narxlarida, O'zbekistondan ham tez ishlaydi.

Server tayyor bo'lgach, sizga uning **IP manzili** va **root parol** (yoki SSH
kalit) beriladi.

## 2-qadam: domenni serverga ulash

Agar domeningiz bo'lsa (masalan `stars-bot.uz`), domen provayderingizning
DNS sozlamalarida **A record** qo'shing:

```
Turi: A
Nom: @  (yoki bot.stars-bot.uz kabi sub-domen uchun "bot")
Qiymat: <VPS IP manzilingiz>
```

DNS o'zgarishi tarqalishi uchun 5-30 daqiqa kuting.

### Domensiz variant (bepul)

Domeningiz bo'lmasa, **sslip.io** xizmatidan foydalaning — hech qanday
sozlashsiz ishlaydigan bepul domen:

```
https://<VPS-IP-tire-bilan>.sslip.io
```

Masalan, VPS IP manzilingiz `95.216.10.42` bo'lsa, domeningiz:
`95-216-10-42.sslip.io` bo'ladi (nuqtalar tire bilan almashtiriladi).
Hech qanday qo'shimcha sozlash kerak emas — bu darhol ishlaydi.

## 3-qadam: serverga ulanish va Docker o'rnatish

SSH orqali serverga ulaning (Windows'da PuTTY yoki Terminal/PowerShell):

```bash
ssh root@<VPS-IP>
```

Docker va Docker Compose'ni o'rnating:

```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
apt install -y docker-compose-plugin git
```

## 4-qadam: kodni serverga olib kelish

```bash
cd /opt
git clone https://github.com/AbdurahmonDev-byte/Bepul_Stars_Robot.git bot
cd bot
```

## 5-qadam: `.env` faylini to'ldirish

```bash
cp .env.example .env
nano .env
```

Quyidagilarni to'ldiring (qiymatlarni Render'dagi eski sozlamalaringizdan
ko'chirib olishingiz mumkin — Render dashboard → Environment):

```
BOT_TOKEN=...                    # BotFather'dan olingan token
ADMIN_IDS=...                    # admin Telegram ID'lari
TURSO_DATABASE_URL=...           # eski Render'dagi bilan BIR XIL qiymat
TURSO_AUTH_TOKEN=...             # eski Render'dagi bilan BIR XIL qiymat
WEBHOOK_URL=https://your-domain.com
PUBLIC_BASE_URL=https://your-domain.com
PORT=8000
```

`your-domain.com` o'rniga 2-qadamda tayyorlagan domeningizni (yoki
sslip.io manzilingizni) yozing. `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`ni
albatta ESKI qiymatlar bilan bir xil qoldiring — shunda barcha
foydalanuvchilar, balanslar va referallar saqlanib qoladi, hech narsa
yo'qolmaydi.

Saqlash uchun: `Ctrl+O`, `Enter`, `Ctrl+X`.

## 6-qadam: `Caddyfile`ga domenni yozish

```bash
nano Caddyfile
```

`your-domain.com` yozuvini xuddi 5-qadamdagi domeningiz bilan almashtiring
(ikkalasida ham AYNAN bir xil bo'lishi kerak), so'ng saqlang.

## 7-qadam: ishga tushirish

```bash
docker compose up -d --build
```

Bir necha soniyadan so'ng bot ishga tushadi, Caddy esa domeningiz uchun
HTTPS sertifikatini avtomatik oladi. Loglarni ko'rish uchun:

```bash
docker compose logs -f bot
```

`Webhook o'rnatildi: https://your-domain.com/webhook` degan qatorni
ko'rsangiz — bot muvaffaqiyatli ishga tushgan. `Ctrl+C` bilan log
ko'rishdan chiqasiz (bot o'zi ishlashda davom etadi).

## 8-qadam: tekshirish

Telegram'da botga `/start` yuboring — avvalgidek ishlashi kerak (barcha
foydalanuvchilar, balanslar saqlangan bo'ladi, chunki Turso o'zgarmagan).
Mini App tugmasini ham bosib ko'ring.

## 9-qadam: Render xizmatini o'chirish

Bot VPS'da to'liq ishlayotganiga ishonch hosil qilgach, Render
dashboard'da eski xizmatni **Suspend** yoki **Delete** qiling — aks holda
ikkala joyda ham ishlab, bir-biriga xalaqit berishi mumkin (ikkita bot
bir vaqtda bitta webhook/tokenni ishlatsa, xatolarga olib keladi).

---

## Kelajakda kodni yangilash

Har safar botga yangi o'zgarish (yangi funksiya, tuzatish) qo'shilganda,
serverda shuni bajarasiz:

```bash
cd /opt/bot
git pull
docker compose up -d --build
```

## Botni to'xtatish / qayta ishga tushirish

```bash
docker compose down      # to'xtatish
docker compose up -d     # qayta ishga tushirish
docker compose restart bot   # faqat botni qayta ishga tushirish (Caddy tegilmaydi)
```

## Muammo bo'lsa

- **"Webhook o'rnatilmadi" xatosi** — domeningiz VPS IP'siga to'g'ri
  yo'naltirilganiga ishonch hosil qiling (`ping your-domain.com` VPS
  IP'ini ko'rsatishi kerak).
- **Caddy HTTPS sertifikat ololmayapti** — domen DNS'i hali tarqalmagan
  bo'lishi mumkin, 10-15 daqiqa kutib qayta urinib ko'ring
  (`docker compose restart caddy`).
- **Ma'lumotlar (foydalanuvchilar) yo'qolganday tuyulsa** — `.env`
  faylidagi `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` eski Render'dagi bilan
  AYNAN bir xil ekanini tekshiring.
