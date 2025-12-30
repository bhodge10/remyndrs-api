# Remyndrs QR Code Implementation Guide

## What You Have

Your updated website now includes a **functional QR code** that automatically opens a text message to your Remyndrs number!

### Files:
1. **index.html** - Updated website with embedded QR code section
2. **remyndrs-qr-branded.png** - Standalone branded image for print/social media

---

## How the QR Code Works

### On the Website (index.html)
The QR code is **dynamically generated** using the QR Server API with your brand colors:

```html
<img src="https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=sms:8555521950&bgcolor=f8f9fa&color=4A90A4&qzone=2">
```

**What this does:**
- Encodes `sms:8555521950` (opens SMS app automatically)
- Uses your brand blue (#4A90A4) for the QR modules
- Light gray background (#f8f9fa)
- 300x300px size (perfect for web)

**When users scan it:**
1. Camera recognizes the QR code
2. Phone prompts "Send message to (855) 552-1950"
3. User taps to open SMS app with your number pre-filled
4. They just type and send!

---

## Customizing the QR Code

### Change the Size:
In `index.html`, find this line and change `size=300x300`:
```html
size=400x400  <!-- Bigger -->
size=200x200  <!-- Smaller -->
```

### Change Colors:
```html
&color=4A90A4    <!-- QR module color (your blue) -->
&bgcolor=f8f9fa  <!-- Background color -->
```

Try:
- `color=50B688` for green QR modules
- `color=3D4F5C` for dark slate modules

### Add a Custom Message:
Want the SMS to pre-fill with text?
```html
data=sms:8555521950?&body=Hi%20Remyndrs!
```
(Note: Works on most phones, but some carriers may not support it)

---

## Using the QR Code Elsewhere

### 1. Print Materials (Business Cards, Flyers, Posters)
Use the **remyndrs-qr-branded.png** file - it has:
- Your branding
- Clear instructions
- Phone number backup
- Professional border

**Print tips:**
- Save as high-res (at least 300 DPI)
- Test scan before mass printing
- Ensure good contrast

### 2. Social Media
Post the branded PNG on:
- Instagram stories
- Facebook posts
- LinkedIn
- Twitter/X

**Caption ideas:**
> "Never forget anything again! Scan to try Remyndrs - your personal memory assistant via SMS 📱"

### 3. Email Signatures
Download a smaller version:
```
https://api.qrserver.com/v1/create-qr-code/?size=150x150&data=sms:8555521950&color=4A90A4
```

### 4. Generate Custom QR Codes Anytime
Use this URL template:
```
https://api.qrserver.com/v1/create-qr-code/
  ?size=SIZE
  &data=sms:YOUR_NUMBER
  &color=HEX_COLOR
  &bgcolor=HEX_BACKGROUND
  &qzone=QUIET_ZONE_SIZE
```

---

## QR Code Best Practices

### ✅ DO:
- Test the QR code on multiple devices before launch
- Ensure high contrast (dark QR on light background)
- Leave white space around the code (quiet zone)
- Make it at least 2cm x 2cm for print
- Include a backup (text phone number)

### ❌ DON'T:
- Make it too small (minimum 2cm for reliable scanning)
- Use low-contrast colors
- Stretch or distort it
- Cover parts with text or images
- Use on glossy surfaces that create glare

---

## Testing Checklist

Before going live, test your QR code:
- [ ] iPhone camera app
- [ ] Android camera app
- [ ] WhatsApp scanner
- [ ] Various lighting conditions
- [ ] Different distances (close and far)

---

## Advanced: Create Your Own Generator

Want to generate QR codes programmatically? Use this Python snippet:

```python
import qrcode

qr = qrcode.QRCode(version=1, box_size=10, border=4)
qr.add_data('sms:8555521950')
qr.make(fit=True)

img = qr.make_image(fill_color="#4A90A4", back_color="white")
img.save('custom-qr.png')
```

---

## Tracking QR Code Usage (Future Feature)

Consider adding UTM parameters or short links to track scans:
1. Create a short link: `remyndrs.com/qr` → redirects to SMS
2. Track clicks in Google Analytics
3. A/B test different placements

---

## Questions?

The QR code on your website is **live and functional** right now. Once you deploy to Netlify, users can immediately start scanning and texting!

**Next Steps:**
1. Deploy the website to Netlify
2. Test the QR code from your phone
3. Share it with your 13 beta testers
4. Start tracking sign-ups!

🎉 You're ready to launch!
