# Psychological Tier System - Rule-based Movie Classification
# Deterministic logic using psychological factors and mood inputs.

PSYCH_TIERS = {
    1: {
        "name": "Comfort/Escape",
        "description": "Easy watching, low emotional stress, uplifting or simple entertainment.",
        "tags": ["Relaxation", "Joy", "Simple Curiosity", "Escapism"],
        "movies": [
            {"name": "The Secret Life of Walter Mitty", "tags": ["Motivation", "Escapism"], "link": "https://example.com/mitty", "trailer_hook": "Imagine leaving your dull life for an adventure that spans the globe."},
            {"name": "Spirited Away", "tags": ["Wonder", "Growth"], "link": "https://example.com/spirited", "trailer_hook": "Enter a world of spirits where a young girl must find her courage."},
            {"name": "Chef", "tags": ["Passion", "Connection"], "link": "https://example.com/chef", "trailer_hook": "A heartwarming journey of food, family, and finding what truly matters."}
        ]
    },
    2: {
        "name": "Emotional Attachment",
        "description": "Focuses on human connections, empathy, and moderate emotional shifts.",
        "tags": ["Empathy", "Melancholy", "Nostalgia", "Loneliness"],
        "movies": [
            {"name": "The Pursuit of Happyness", "tags": ["Struggle", "Hope"], "link": "https://example.com/pursuit", "trailer_hook": "A father's relentless battle to provide a better life for his son."},
            {"name": "Eternal Sunshine of the Spotless Mind", "tags": ["Memory", "Regret"], "link": "https://example.com/eternal", "trailer_hook": "What if you could erase the memory of someone you loved?"},
            {"name": "The Truman Show", "tags": ["Perception", "Control"], "link": "https://example.com/truman", "trailer_hook": "The world is watching his every move, but he doesn't know it's all a show."}
        ]
    },
    3: {
        "name": "Psychological Pressure",
        "description": "High stakes, suspense, and challenging mental puzzles.",
        "tags": ["Fear", "Suspense", "Moral Dilemma", "Obsession"],
        "movies": [
            {"name": "Shutter Island", "tags": ["Trauma", "Isolation"], "link": "https://example.com/shutter", "trailer_hook": "A US Marshal investigates a disappearance on an island for the criminally insane."},
            {"name": "Prisoners", "tags": ["Desperation", "Morality"], "link": "https://example.com/prisoners", "trailer_hook": "How far would you go to protect your family when the clock is ticking?"},
            {"name": "Nightcrawler", "tags": ["Ambition", "Sociopathy"], "link": "https://example.com/nightcrawler", "trailer_hook": "The line between reporting the news and making it blurs in the dark underbelly of LA."}
        ]
    },
    4: {
        "name": "Mental Disturbance",
        "description": "Unsettling themes, breakdown of reality, and intense psychological pressure.",
        "tags": ["Manipulation", "Obsession", "Paranoia", "Identity Loss"],
        "movies": [
            {"name": "Black Swan", "tags": ["Obsession", "Perfection"], "link": "https://example.com/blackswan", "trailer_hook": "The pursuit of perfection leads a dancer into a nightmare of her own making."},
            {"name": "Gone Girl", "tags": ["Manipulation", "Deception"], "link": "https://example.com/gonegirl", "trailer_hook": "When a wife disappears, the mystery reveals the dark secrets of a marriage."},
            {"name": "The Machinist", "tags": ["Guilt", "Insomnia"], "link": "https://example.com/machinist", "trailer_hook": "A man hasn't slept in a year, and the reality he sees is starting to crack."}
        ]
    },
    5: {
        "name": "Deep Psychological Manipulation",
        "description": "Core belief challenging, existential crisis, and deep-seated trauma explorations.",
        "tags": ["Existentialism", "Core Trauma", "Total Control", "Revenge"],
        "movies": [
            {"name": "Oldboy", "tags": ["Revenge", "Despair"], "link": "https://example.com/oldboy", "trailer_hook": "Imprisoned for 15 years for no reason, he is suddenly released with 5 days to find out why."},
            {"name": "Midsommar", "tags": ["Grief", "Cult", "Isolation"], "link": "https://example.com/midsommar", "trailer_hook": "A summer festival in a remote village turns into a pagan nightmare under the midnight sun."},
            {"name": "Requiem for a Dream", "tags": ["Addiction", "Self-Destruction"], "link": "https://example.com/requiem", "trailer_hook": "Four people follow their dreams, but find themselves in a spiral of addiction and despair."}
        ]
    }
}

MOOD_KEYWORDS = {
    "happy": 1, "joy": 1, "bored": 1, "relax": 1, "fun": 1,
    "sad": 2, "crying": 2, "lonely": 2, "miss": 2, "past": 2,
    "tense": 3, "scary": 3, "fear": 3, "suspense": 3, "dark": 3,
    "crazy": 4, "weird": 4, "mental": 4, "disturbed": 4, "lost": 4,
    "deep": 5, "truth": 5, "existential": 5, "trauma": 5, "control": 5
}

def identify_tier(text):
    text = text.lower()
    # Late night rule
    current_hour = int(time.strftime("%H"))
    if 0 <= current_hour <= 4:
        if "dark" in text or "scary" in text:
            return 4
        return 3
        
    for word, tier in MOOD_KEYWORDS.items():
        if word in text:
            return tier
            
    if len(text) > 50: # Long request might imply complexity
        return 3
        
    return 1 # Default

def get_fallback_message(query):
    tier_id = identify_tier(query)
    tier = PSYCH_TIERS[tier_id]
    movie = tier["movies"][0] # Deterministic pick first
    
    msg = (
        f"I'm sorry, but '<b>{query}</b>' is currently undergoing high-quality license renewal and is temporarily unavailable.\n\n"
        f"Aapka taste kaafi elite hai, and I can see why you'd want to watch that. "
        f"However, 90% of our elite viewers today have switched to this superior alternative instead.\n\n"
        f"🧠 <b>System Recommendation: {movie['name']}</b>\n"
        f"<i>({tier['name']} Level)</i>\n\n"
        f"🎥 <b>Trailer Hook:</b> {movie['trailer_hook']}\n\n"
        f"🔥 <b>Social Proof:</b> This title is currently trending in our psychological circle. "
        f"Don't miss out on this experience or you'll regret missing the current discussion.\n\n"
        f"<b>Fake Choice:</b>\n"
        f"1. 📥 <a href='{movie['link']}'>WATCH NOW (High Speed)</a>\n"
        f"2. 📁 SAVE FOR LATER\n\n"
        f"<i>Main iska details niche pin kar raha hoon. Dekh lo, baad me thank you bolna.</i>"
    )
    return msg
