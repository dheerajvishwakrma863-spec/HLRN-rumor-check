"""
seed_data.py - Pre-load HLRN's Layer-1 knowledge base with demo rumours.

Usage
-----
    python seed_data.py            # add / refresh the demo entries (idempotent)
    python seed_data.py --fresh    # delete the database file first, then seed
    python seed_data.py --no-verify

What it does
------------
Writes ~14 realistic Indian viral-forward rumours (Hindi / Hinglish / English) as
*admin-verified* records, i.e. exactly what a moderator override produces. Anything that
matches them afterwards is answered by Layer 1: no AI call, no tokens, milliseconds.
At the end it self-tests: every rumour is wrapped in typical WhatsApp "forwarded" noise and
looked up again, and the average lookup time is printed.

IMPORTANT - read before presenting
----------------------------------
* These entries are DEMO CONTENT written for the hackathon. Wording is deliberately cautious
  ("not confirmed by any official source"), but you must check each one against current
  PIB Fact Check / ministry pages before presenting it as a real fact-check.
* The source links are official portal HOME pages (real, stable domains), not links to
  specific fact-check articles. Replace them with exact article URLs for extra credibility.
* Rules such as UPI fees or voting methods can change; the explanations say so.
* Layer 1 is lexical (token + character similarity), NOT semantic: it recognises re-forwards
  of the same text (with emojis, "forwarded" tags, spelling variants), not paraphrases or
  translations. For the live demo paste the texts from PITCH_GUIDE.md.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):          # Windows consoles: avoid UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import settings                                   # noqa: E402
from database import STATUS_ADMIN, Database                   # noqa: E402
from preprocessing import detect_language, preprocess, redact_pii, truncate   # noqa: E402

FAKE = "Fake / Misleading"
TRUE = "Verified True"
REVIEW = "Needs Human Review / Unverified"

# Official portals (home pages). tier: official | fact_checker | other
PIB = ("PIB Fact Check", "https://factcheck.pib.gov.in", "factcheck.pib.gov.in", "official")
CYBER = ("National Cyber Crime Reporting Portal", "https://cybercrime.gov.in", "cybercrime.gov.in", "official")
RBI = ("Reserve Bank of India", "https://www.rbi.org.in", "rbi.org.in", "official")
NPCI = ("NPCI", "https://www.npci.org.in", "npci.org.in", "official")
WHO = ("World Health Organization", "https://www.who.int", "who.int", "official")
MOHFW = ("Ministry of Health & Family Welfare", "https://www.mohfw.gov.in", "mohfw.gov.in", "official")
DOT = ("Department of Telecommunications", "https://dot.gov.in", "dot.gov.in", "official")
MEITY = ("Ministry of Electronics & IT", "https://www.meity.gov.in", "meity.gov.in", "official")
PMKISAN = ("PM-KISAN Portal", "https://pmkisan.gov.in", "pmkisan.gov.in", "official")
RAILWAYS = ("Indian Railways", "https://indianrailways.gov.in", "indianrailways.gov.in", "official")
ECI = ("Election Commission of India", "https://eci.gov.in", "eci.gov.in", "official")
VOTERS = ("ECI Voter Portal", "https://voters.eci.gov.in", "voters.eci.gov.in", "official")
UIDAI = ("UIDAI", "https://uidai.gov.in", "uidai.gov.in", "official")
NASA = ("NASA", "https://www.nasa.gov", "nasa.gov", "other")


def R(text, verdict, category, explanation, action, evidence, sources, confidence=99.0) -> Dict:
    """Small constructor to keep the table below readable."""
    return dict(text=text, verdict=verdict, category=category, explanation=explanation,
                action=action, evidence=evidence, sources=sources, confidence=confidence)


RUMOURS: List[Dict] = [
    # 1 ------------------------------------------------------------------ Hinglish | scheme / phishing
    R("Sarkar ki taraf se sabhi students ko free laptop mil raha hai. PM Free Laptop Yojana mein abhi "
      "register karein is link par aur apna Aadhaar number bharein.",
      FAKE, "Government Schemes",
      "Sarkari yojanaon ki jaankari official .gov.in websites aur PIB ke zariye hi di jati hai. Kai log yeh "
      "message achhe irade se share karte hain, lekin 'sabhi students ko free laptop' wala yeh daava kisi "
      "official sarkari ghoshna se pushta nahi hota. Aise link par Aadhaar number bharne se aapki personal "
      "jaankari ka galat istemal ho sakta hai.",
      "Link par Aadhaar ya bank details na bharein. Yojana ki pushti sambandhit vibhag ki .gov.in website ya "
      "PIB Fact Check par karein, aur message aage forward na karein.",
      ["Central schemes are announced on official .gov.in portals and PIB, not through forwarded links.",
       "Forms that collect Aadhaar numbers on unofficial links are a common phishing pattern."],
      [PIB, CYBER]),

    # 2 ------------------------------------------------------------------ Hindi | free recharge
    R("सरकार सभी मोबाइल उपभोक्ताओं को 3 महीने का मुफ्त रिचार्ज दे रही है। नीचे दिए गए लिंक पर क्लिक करके "
      "अभी रजिस्टर करें।",
      FAKE, "Finance / Scams",
      "सरकारी घोषणाएँ आधिकारिक सरकारी वेबसाइटों और PIB के माध्यम से ही की जाती हैं। बहुत से लोग यह संदेश नेक नीयत से "
      "साझा करते हैं, लेकिन 'सभी को 3 महीने का मुफ्त रिचार्ज' वाला दावा किसी आधिकारिक घोषणा से पुष्ट नहीं है। ऐसे "
      "लिंक अक्सर निजी जानकारी या पैसे हड़पने के लिए बनाए जाते हैं।",
      "लिंक पर क्लिक न करें और अपनी जानकारी न भरें। संदेश आगे न भेजें। ठगी की शिकायत cybercrime.gov.in पर या "
      "हेल्पलाइन 1930 पर करें।",
      ["No official announcement of a blanket free-recharge scheme is available on government portals.",
       "Link-based 'register now' offers are a known route for data theft."],
      [PIB, CYBER]),

    # 3 ------------------------------------------------------------------ English | bank KYC phishing
    R("Dear customer, your bank account will be blocked today. Update your KYC immediately by clicking "
      "the link below or call this number to avoid suspension.",
      FAKE, "Finance / Scams",
      "Banks and the RBI advise customers never to share account details, OTPs or passwords through links or "
      "phone calls. Many people forward alerts like this out of concern, but a message threatening immediate "
      "account blocking along with a link follows a well-known phishing pattern and is not confirmed by any "
      "official source. Genuine KYC updates are done through your bank's official app, website or branch.",
      "Do not click the link or call the number. Contact your bank through its official app or helpline, and "
      "report fraud at cybercrime.gov.in or on 1930.",
      ["RBI repeatedly warns customers not to share credentials via links or unsolicited calls.",
       "Urgent 'account will be blocked' threats are a standard phishing tactic."],
      [RBI, CYBER]),

    # 4 ------------------------------------------------------------------ English | 5G / health
    R("5G towers are spreading coronavirus and the government is hiding the truth. Stop the 5G towers in "
      "your area and share this with everyone.",
      FAKE, "Health / Medical",
      "Viruses cannot travel on radio waves or mobile networks. The WHO states that COVID-19 spreads between "
      "people through respiratory droplets and close contact, not through 5G or other mobile networks. Many "
      "people shared this out of worry during the pandemic, but the link between 5G and the virus has been "
      "rejected by health and telecom authorities. Health advisories from the WHO and the Ministry of Health "
      "are the best place to check.",
      "Please rely on advisories from the WHO and the Ministry of Health and avoid forwarding this message.",
      ["The WHO says COVID-19 spreads through respiratory droplets and contact, not mobile networks.",
       "Radio waves cannot carry a virus."],
      [WHO, MOHFW, DOT]),

    # 5 ------------------------------------------------------------------ Hinglish | WhatsApp ticks
    R("Dhyan dein! WhatsApp par agar teen blue tick aa jaye to iska matlab hai ki sarkar ne aapka message "
      "dekh liya hai aur aap par action ho sakta hai. Sabko bata dijiye.",
      FAKE, "Technology / Cyber",
      "WhatsApp mein message ke saath ek ya do tick dikhte hain, jo grey ya blue ho sakte hain, aur ye sirf "
      "delivery aur read status batate hain. Kai log darr ki wajah se yeh message share karte hain, lekin "
      "'teen blue tick matlab sarkari action' wala daava sahi nahi hai, kyunki teen blue tick jaisa koi "
      "feature hai hi nahi.",
      "Is message ko aage forward na karein. WhatsApp ke features ki sahi jaankari uske Help Center par dekhein.",
      ["WhatsApp shows one or two ticks (grey or blue) indicating sent, delivered and read.",
       "There is no 'three blue ticks' feature."],
      [PIB, MEITY]),

    # 6 ------------------------------------------------------------------ Hinglish | child-lifting rumour
    R("Savdhan! Aapke ilake mein bachche uthane wala gang ghoom raha hai. Ye video sabhi groups mein turant "
      "bhejein taaki bachche surakshit rahein.",
      REVIEW, "Crime / Public Safety",
      "Bachchon ki suraksha bahut zaroori hai, isliye log aise message achhe irade se bhejte hain. Hum is "
      "message ko kisi official source se confirm ya khandit nahi kar paaye. Bina pushti ke 'bachche uthane "
      "wala gang' wale videos aksar purane ya kisi aur jagah ke hote hain, aur aise forwards ke baad bhid "
      "dwara galat logon par hamle bhi hue hain. Kisi bhi sandehjanak ghatna ki jaankari seedhe police ko "
      "dena sabse surakshit tareeka hai.",
      "Video ko aage forward na karein. Kuch bhi sandehjanak lage to 112 par police ko turant batayein.",
      ["Unverified 'child-lifting gang' forwards have previously triggered mob violence in India.",
       "The specific video and location in this message could not be verified."],
      [CYBER], 85.0),

    # 7 ------------------------------------------------------------------ Hindi | star-series notes
    R("500 के जिन नोटों पर स्टार (*) का निशान है वे नकली हैं और बैंक में नहीं चलेंगे। सभी को बताएं।",
      FAKE, "Finance / Scams",
      "भारतीय रिज़र्व बैंक के अनुसार स्टार (*) सीरीज़ वाले बैंकनोट वैध मुद्रा हैं। ये नोट छपाई में खराब हुए नोटों की "
      "जगह जारी किए जाते हैं, इसलिए स्टार का निशान नकली होने का संकेत नहीं है। कई लोग भ्रम में यह संदेश साझा कर देते हैं।",
      "स्टार वाले नोट बिना डर के चलाएँ। किसी भी संदेह पर RBI की वेबसाइट या अपने बैंक से जानकारी लें।",
      ["RBI states that star-series banknotes are legal tender.",
       "The star replaces a misprinted note in the series; it is not a mark of a counterfeit."],
      [RBI]),

    # 8 ------------------------------------------------------------------ English | UPI charges
    R("From next month UPI payments will be charged 1.1% on every transaction. Government will levy a fee on "
      "all UPI users, so keep cash ready.",
      FAKE, "Finance / Scams",
      "In 2023 NPCI clarified that ordinary UPI payments between bank accounts remain free for customers. "
      "The 1.1% interchange fee that was widely reported applies only to certain merchant payments made with "
      "prepaid payment instruments such as wallets, and it is borne by the merchant side, not charged to the "
      "customer. Rules can change, so the latest position is always on NPCI's official website.",
      "There is no need to stock up on cash. Check NPCI's official website for the latest rules before acting "
      "on such messages.",
      ["NPCI clarified that bank-account-to-bank-account UPI remains free for users.",
       "The 1.1% interchange applies to specific merchant PPI transactions, not to customers."],
      [NPCI, PIB]),

    # 9 ------------------------------------------------------------------ Hinglish | PM Kisan APK
    R("PM Kisan ka naya app aaya hai. Ye APK file download karke install karein aur 2000 rupaye seedhe apne "
      "account mein payein.",
      FAKE, "Technology / Cyber",
      "PM-KISAN ki jaankari aur registration official portal pmkisan.gov.in aur verified official app ke zariye "
      "hi hote hain. Kai kisan bhai yeh message achhe irade se bhejte hain, lekin WhatsApp par aayi APK file "
      "install karna surakshit nahi hai, kyunki aise nakli apps phone se OTP aur bank jaankari churane ke liye "
      "banaye jate hain.",
      "APK file install na karein. Jaankari sirf pmkisan.gov.in ya Google Play ke verified official app se lein. "
      "Dhokhadhadi hone par cybercrime.gov.in ya 1930 par report karein.",
      ["Scheme services are offered through the official portal and verified apps.",
       "APK files shared on chat apps are a common malware route for stealing OTPs."],
      [PMKISAN, CYBER]),

    # 10 ----------------------------------------------------------------- Hinglish | job scam
    R("Railway mein bina exam ke permanent naukri! 50000 rupaye jama karein is number par, joining letter "
      "7 din mein ghar pahunch jayega.",
      FAKE, "Education / Jobs",
      "Sarkari bharti sirf official notification aur selection process ke zariye hoti hai, aur Railway kisi bhi "
      "pad ke liye kisi phone number par paise jama karne ko nahi kehta. Naukri ki talash mein log aise message "
      "par bharosa kar lete hain, isliye savdhan rehna zaroori hai.",
      "Paise na bhejein. Bharti ki jaankari Railway ki official website par dekhein aur thagi ki report "
      "cybercrime.gov.in par karein.",
      ["Government recruitment runs through official notifications and examinations.",
       "Asking for money against a 'guaranteed' job is a classic employment scam."],
      [RAILWAYS, CYBER]),

    # 11 ----------------------------------------------------------------- Hinglish | voting via WhatsApp
    R("Election Commission ne announce kiya hai ki ab aap WhatsApp ya SMS se bhi vote de sakte hain. Polling "
      "booth jaane ki zaroorat nahi, apna Voter ID number bhej dein.",
      FAKE, "Politics / Elections",
      "Matdaan ke tareeke Election Commission ki official website par hi ghoshit hote hain. Abhi aam "
      "matdaataon ke liye vote polling booth par EVM ke zariye hi dala jata hai (kuch vishesh shreniyon ke "
      "liye postal ballot ki suvidha hai). WhatsApp ya SMS se vote dene ki suvidha nahi hai, aur Voter ID "
      "number bhejne ki bhi zaroorat nahi. Sabse naye niyam ECI ki website par dekhein.",
      "Voter ID number ya OTP kisi ko na bhejein. Matdaan ki jaankari eci.gov.in par dekhein.",
      ["Voting for general electors is conducted at polling stations.",
       "The Election Commission announces voting methods on its official channels."],
      [ECI, VOTERS]),

    # 12 ----------------------------------------------------------------- English | TRUE example
    R("You can download your digital Voter ID card (e-EPIC) for free from the Election Commission's voter "
      "portal using your EPIC number or form reference number and a registered mobile number.",
      TRUE, "Government Schemes",
      "Yes. The Election Commission of India offers e-EPIC, a digital version of the Voter ID card, that "
      "registered voters can download from the official voter portal using a registered mobile number. You do "
      "not need to pay anyone to get it. Please use only the official ECI portal and avoid third-party sites "
      "that ask for payment.",
      "Download it only from the official ECI voter portal and never share your OTP with anyone.",
      ["ECI provides e-EPIC downloads on its voter portal.",
       "Download requires a mobile number registered with the electoral roll record."],
      [VOTERS, ECI]),

    # 13 ----------------------------------------------------------------- Hinglish | TRUE example
    R("Aadhaar PVC card UIDAI ki official website se online order kar sakte hain. Isme aapke registered "
      "mobile number par OTP aata hai aur thoda shulk lagta hai.",
      TRUE, "Government Schemes",
      "Haan, UIDAI ki official website se Aadhaar PVC card online order kiya ja sakta hai. Iske liye registered "
      "mobile number par OTP aata hai aur ek nishchit shulk dena hota hai. Sirf uidai.gov.in jaise official "
      "portal ka istemal karein aur kisi third-party agent ko OTP ya Aadhaar ki jaankari na dein.",
      "Order sirf official UIDAI website se karein aur OTP kisi ke saath share na karein.",
      ["UIDAI offers online ordering of the Aadhaar PVC card.",
       "The order is verified with an OTP sent to the registered mobile number."],
      [UIDAI]),

    # 14 ----------------------------------------------------------------- English | science hoax
    R("NASA has confirmed that the earth will go completely dark for 15 days due to a rare planetary "
      "alignment. Stock food and water and keep your phones charged.",
      FAKE, "Science / General Knowledge",
      "No such event has been announced by NASA or any other space agency, and NASA publishes its "
      "announcements on its official website. Similar 'days of darkness' messages have circulated for years and "
      "have been debunked by scientists. A planetary alignment does not switch off sunlight on Earth. For "
      "astronomy news, official space-agency pages are the most reliable source.",
      "There is no need to stock up. Please check official space-agency websites before forwarding such news.",
      ["Planetary alignments do not block sunlight from reaching Earth.",
       "The same 'days of darkness' hoax has circulated repeatedly and was debunked."],
      [NASA]),
]


def noisy_variant(text: str) -> str:
    """What a real viral re-forward looks like: tags, emojis, shouting, call-to-share."""
    return f"*Forwarded many times*\n🚨🚨 {text} 🙏🙏\nPlease share with everyone"


def seed(db: Database) -> int:
    for r in RUMOURS:
        pre = preprocess(r["text"], settings.max_input_chars)
        tag, _ = detect_language(r["text"])
        db.save_claim(
            input_type="text", verdict=r["verdict"], confidence=r["confidence"], category=r["category"],
            recommended_action=r["action"], explanation=r["explanation"], evidence=r["evidence"],
            sources=[{"title": t, "url": u, "domain": d, "trust_tier": tier} for t, u, d, tier in r["sources"]],
            language_tag=tag, status=STATUS_ADMIN, origin="admin",
            sample_text="[seed] " + redact_pii(truncate(pre.cleaned, 1000)),
            text_hash=pre.text_hash, norm_text=pre.normalized, tokens=pre.tokens,
        )
    db.log_admin_action("seed_data.py", None, "seed", f"{len(RUMOURS)} demo entries")
    return len(RUMOURS)


def verify(db: Database) -> bool:
    """Look every rumour up again (with forwarding noise) exactly like Layer 1 does."""
    ok, times = 0, []
    print(f"\n{'#':>2}  {'match':<6} {'ms':>6}  {'verdict':<32} text")
    for i, r in enumerate(RUMOURS, 1):
        pre = preprocess(noisy_variant(r["text"]), settings.max_input_chars)
        t0 = time.perf_counter()
        row = db.find_exact_text(pre.text_hash)
        kind = "exact"
        if row is None:
            fz = db.find_fuzzy_text(pre.normalized, pre.tokens, settings.fuzzy_threshold, settings.fuzzy_min_tokens)
            row, kind = (fz[0], "fuzzy") if fz else (None, "MISS")
        ms = (time.perf_counter() - t0) * 1000
        times.append(ms)
        good = row is not None and row["verdict"] == r["verdict"]
        ok += good
        print(f"{i:>2}  {kind:<6} {ms:>6.1f}  {r['verdict']:<32} {truncate(r['text'], 48)}")
    avg = sum(times) / len(times)
    print(f"\nSelf-test: {ok}/{len(RUMOURS)} matched · avg lookup {avg:.1f} ms · max {max(times):.1f} ms")
    return ok == len(RUMOURS)


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the HLRN demo database.")
    ap.add_argument("--fresh", action="store_true", help="delete the database file before seeding")
    ap.add_argument("--no-verify", action="store_true", help="skip the self-test")
    args = ap.parse_args()

    path = settings.db_path
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass
        print(f"Removed existing database: {path}")

    db = Database(path)
    n = seed(db)
    print(f"Seeded {n} admin-verified rumours into {os.path.abspath(path)}")
    if args.no_verify:
        return 0
    return 0 if verify(db) else 1


if __name__ == "__main__":
    sys.exit(main())
