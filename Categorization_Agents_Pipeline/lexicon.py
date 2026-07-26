"""The pipeline's knowledge: what words mean what.

There is no model here by design — the agents are deterministic, so the same bio
always yields the same verdict and a wrong tag is a line you can find and fix
rather than a prompt you have to coax. That makes THIS file the accuracy of the
whole pipeline, and the one place to extend it: add a term, get better results,
change no code.

Two rules the entries follow:

  * Terms are matched on WORD BOUNDARIES, so "art" cannot fire inside "party"
    and "pt" cannot fire inside "captain". Anything that would still be
    ambiguous as a bare word (e.g. "ac", "pk") is left out rather than risked.
  * A multi-word entry is a phrase and scores higher than a single word, because
    "real estate" is unambiguous where "estate" alone is not.
"""
from __future__ import annotations

# ── Categories ───────────────────────────────────────────────────────────
# The canonical set. Keep names word-distinct: the classifier reports them
# verbatim, and two categories sharing a word makes a verdict hard to read.
CATEGORIES: tuple[str, ...] = (
    "Travel", "Sports", "Fitness", "Food", "Fashion", "Beauty", "Business",
    "Entrepreneurship", "Technology", "Photography", "Music", "Gaming",
    "Education", "Finance", "Pets", "Cars", "Art", "Lifestyle", "Parenting",
    "Health", "Luxury", "Real Estate",
    # Added after reading the real set: film/TV/drama pages, news outlets and
    # devotional accounts are all common in it and had nowhere honest to go.
    "Entertainment", "News", "Religion",
)

# category -> the words and phrases that mean it.
CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "Travel": (
        "travel", "traveler", "traveller", "travelling", "traveling", "wanderlust",
        "explore", "explorer", "exploring", "adventure", "adventurer", "backpacking",
        "backpacker", "nomad", "digital nomad", "globetrotter", "world traveler",
        "tourism", "tourist", "trip", "trips", "roadtrip", "road trip", "journey",
        "voyage", "destination", "destinations", "itinerary", "vanlife", "van life",
        "expat", "hiking", "trekking", "trek", "mountaineering", "safari", "cruise",
        "passport", "boarding pass", "jetsetter", "travel blogger", "travel guide",
        "tour operator", "tour guide", "visited", "countries visited",
    ),
    # Deliberately NOT here: coach, trainer, team, player, training, match,
    # captain. Every one of them is used far more often outside sport (business
    # coach, corporate training, "team" in a company bio) than in it, and a
    # genuinely sporting bio nearly always names its sport — which SPORTS picks
    # up and FACET_CATEGORY turns into Sports anyway.
    "Sports": (
        "sport", "sports", "athlete", "athletics", "sportsman", "sportswoman",
        "league", "tournament", "championship", "sportswear", "referee",
        "sports coach", "team captain",
    ),
    "Fitness": (
        "fitness", "gym", "workout", "bodybuilding", "bodybuilder", "lifting",
        "weightlifting", "powerlifting", "crossfit", "calisthenics", "personal trainer",
        "fitness coach", "gym rat", "fit", "shredded", "muscle", "abs", "cardio",
        "hiit", "pilates", "yoga", "zumba", "aerobics", "strength training",
        "fat loss", "transformation coach",
    ),
    "Food": (
        "food", "foodie", "foodies", "chef", "cook", "cooking", "recipe", "recipes",
        "baking", "baker", "pastry", "cuisine", "restaurant", "cafe", "coffee",
        "barista", "bbq", "grill", "vegan", "vegetarian", "halal", "dessert",
        "chocolate", "pizza", "burger", "sushi", "street food", "home cook",
        "food blogger", "kitchen", "catering", "bakery",
    ),
    "Fashion": (
        "fashion", "style", "stylist", "outfit", "ootd", "wardrobe", "clothing",
        "apparel", "streetwear", "sneakers", "sneakerhead", "designer", "couture",
        "boutique", "thrift", "trends", "lookbook", "fashion blogger", "model",
        "modeling", "modelling", "runway", "tailor", "textile",
    ),
    "Beauty": (
        "beauty", "makeup", "mua", "makeup artist", "skincare", "cosmetics",
        "hair", "hairstylist", "haircut", "barber", "salon", "nails", "nail art",
        "lashes", "eyelash", "brows", "microblading", "spa", "facial", "glam",
        "beautician", "aesthetician",
    ),
    "Business": (
        "business", "ceo", "coo", "cfo", "founder", "cofounder", "co-founder",
        "director", "manager", "consultant", "consulting", "agency", "b2b",
        "corporate", "enterprise", "sales", "marketing", "advertising", "branding",
        "brand strategist", "ecommerce", "e-commerce", "dropshipping", "wholesale",
        "supplier", "distributor", "trader", "trading", "import", "export",
    ),
    "Entrepreneurship": (
        "entrepreneur", "entrepreneurship", "startup", "start-up", "solopreneur",
        "hustler", "hustle", "self made", "self-made", "side hustle", "bootstrapped",
        "founder", "building in public", "indie hacker", "business owner",
    ),
    "Technology": (
        "tech", "technology", "software", "developer", "programmer", "coding",
        "coder", "engineer", "engineering", "web developer", "full stack",
        "frontend", "backend", "devops", "data science", "data scientist",
        "machine learning", "artificial intelligence", "ai", "blockchain",
        "cybersecurity", "it", "saas", "app developer", "python", "javascript",
        "react", "android", "ios", "cloud", "linux", "database",
    ),
    "Photography": (
        "photography", "photographer", "photo", "photos", "photographe",
        "videography", "videographer", "cinematography", "cinematographer",
        "filmmaker", "filmmaking", "camera", "lens", "portrait", "portraits",
        "wedding photographer", "photoshoot", "shoot", "editing", "lightroom",
        "visuals", "content creator", "creator", "reels", "drone",
    ),
    "Music": (
        "music", "musician", "singer", "song", "songs", "songwriter", "vocalist",
        "rapper", "rap", "hip hop", "hiphop", "producer", "beats", "dj",
        "guitar", "guitarist", "piano", "pianist", "drummer", "violin", "band",
        "concert", "spotify", "soundcloud", "melody", "lyrics", "composer",
        "qawwali", "ghazal", "sufi",
    ),
    "Gaming": (
        "gaming", "gamer", "esports", "e-sports", "streamer", "twitch", "playstation",
        "xbox", "pubg", "fortnite", "valorant", "minecraft", "call of duty",
        "league of legends", "dota", "csgo", "counter strike", "fifa", "gameplay",
        "speedrun", "console", "pc gaming",
    ),
    "Education": (
        "education", "teacher", "teaching", "tutor", "tutoring", "professor",
        "lecturer", "student", "school", "college", "university", "academy",
        "institute", "scholar", "scholarship", "phd", "mphil", "masters",
        "bachelor", "graduate", "undergraduate", "learning", "study", "studying",
        "exam", "coaching center", "mentor", "course", "courses",
    ),
    "Finance": (
        "finance", "financial", "investor", "investing", "investment", "stocks",
        "stock market", "trading", "trader", "forex", "crypto", "cryptocurrency",
        "bitcoin", "ethereum", "portfolio", "wealth", "banking", "banker",
        "accountant", "accounting", "audit", "tax", "insurance", "mutual funds",
        "passive income", "financial freedom",
    ),
    "Pets": (
        "pet", "pets", "dog", "dogs", "puppy", "cat", "cats", "kitten", "animal",
        "animals", "veterinarian", "vet", "aquarium", "fish keeping", "parrot",
        "birds", "horse", "equestrian", "rescue", "adoption", "pet lover",
        "dog lover", "cat lover", "kennel", "cattery",
    ),
    "Cars": (
        "car", "cars", "automotive", "auto", "vehicle", "vehicles", "motor",
        "motors", "driving", "driver", "racing", "motorsport", "supercar",
        "sportscar", "jdm", "bike", "biker", "motorcycle", "superbike", "rider",
        "garage", "mechanic", "car dealer", "showroom", "detailing", "tuning",
        "car enthusiast", "car lover", "rental cars",
    ),
    "Art": (
        "art", "artist", "artwork", "painting", "painter", "drawing", "sketch",
        "illustration", "illustrator", "graphic design", "designer", "digital art",
        "calligraphy", "sculpture", "gallery", "exhibition", "creative", "craft",
        "handmade", "diy", "tattoo", "tattoo artist", "animation", "animator",
    ),
    "Lifestyle": (
        "lifestyle", "daily life", "vlog", "vlogger", "blogger", "influencer",
        "life", "living", "motivation", "motivational", "inspiration", "mindset",
        "self improvement", "self-improvement", "personal growth", "minimalist",
        "minimalism", "positivity", "happiness", "journaling",
    ),
    "Parenting": (
        "parenting", "parent", "mom", "mum", "mother", "dad", "father", "momlife",
        "mom life", "dadlife", "girl mom", "boy mom", "kids", "children", "baby",
        "babies", "toddler", "newborn", "pregnancy", "pregnant", "maternity",
        "homeschool", "homeschooling", "family",
    ),
    "Health": (
        "health", "healthy", "wellness", "wellbeing", "well-being", "nutrition",
        "nutritionist", "dietitian", "diet", "doctor", "physician", "surgeon",
        "nurse", "medical", "medicine", "clinic", "hospital", "dentist", "dental",
        "therapy", "therapist", "physiotherapy", "physiotherapist", "psychology",
        "psychologist", "mental health", "meditation", "mindfulness", "healing",
        "pharmacist", "pharmacy", "mbbs",
    ),
    "Luxury": (
        "luxury", "luxurious", "premium", "exclusive", "vip", "elite", "bespoke",
        "high end", "high-end", "designer brands", "yacht", "private jet",
        "first class", "penthouse", "millionaire", "billionaire", "opulent",
    ),
    "Entertainment": (
        "movie", "movies", "film", "films", "cinema", "series", "drama", "dramas",
        "tv show", "netflix series", "actor", "actress", "casting", "trailer",
        "box office", "imax", "theatre", "theater", "comedy", "comedian",
        "standup", "stand-up", "celebrity", "fan page", "fanpage", "edits",
        "kdrama", "anime", "bollywood", "hollywood", "lollywood", "web series",
    ),
    "News": (
        "news", "breaking news", "headlines", "journalism", "journalist",
        "reporter", "press", "media house", "newspaper", "bulletin", "coverage",
        "daily updates", "news agency", "current affairs", "politics",
        "political", "correspondent",
    ),
    "Religion": (
        "islam", "islamic", "muslim", "quran", "hadith", "sunnah", "allah",
        "alhamdulillah", "inshallah", "masjid", "mosque", "dawah", "deen",
        "hijab", "shia", "sunni", "naat", "azkar", "christ", "christian",
        "jesus", "church", "bible", "gospel", "pastor", "ministry", "worship",
        "hindu", "mahadev", "krishna", "ram", "bhakti", "mandir", "sikh",
        "waheguru", "gurudwara", "spiritual", "faith", "prayer", "blessed",
    ),
    "Real Estate": (
        "real estate", "realestate", "realtor", "property", "properties",
        "estate agent", "broker", "housing", "apartments", "plots", "plot for sale",
        "commercial property", "residential", "landlord", "leasing", "rentals",
        "construction", "builder", "developer society", "architecture", "architect",
        "interior design", "interior designer",
    ),
}

# Emoji are how a great many bios actually say what they are. Weak on their own
# (see config.WEIGHT["emoji"]) but decisive when they agree with a word.
CATEGORY_EMOJI: dict[str, str] = {
    "✈": "Travel", "🌍": "Travel", "🌎": "Travel", "🌏": "Travel", "🧳": "Travel",
    "🗺": "Travel", "⛰": "Travel", "🏔": "Travel", "🏕": "Travel", "🏝": "Travel",
    "🚁": "Travel", "🛫": "Travel", "🛩": "Travel", "🏞": "Travel",
    "⚽": "Sports", "🏀": "Sports", "🏏": "Sports", "🏈": "Sports", "🎾": "Sports",
    "🏐": "Sports", "🏸": "Sports", "🥊": "Sports", "⛳": "Sports", "🏓": "Sports",
    "🥇": "Sports", "🏆": "Sports", "🏅": "Sports",
    "💪": "Fitness", "🏋": "Fitness", "🤸": "Fitness", "🧘": "Fitness", "🏃": "Fitness",
    "🍕": "Food", "🍔": "Food", "🍜": "Food", "🍰": "Food", "☕": "Food", "🍳": "Food",
    "🧁": "Food", "🍫": "Food", "🥗": "Food", "🍽": "Food", "👨‍🍳": "Food",
    "👗": "Fashion", "👠": "Fashion", "👜": "Fashion", "🕶": "Fashion", "🧵": "Fashion",
    "💄": "Beauty", "💅": "Beauty", "💇": "Beauty", "✂": "Beauty", "🪒": "Beauty",
    "💼": "Business", "📈": "Business", "🤝": "Business", "🏢": "Business",
    "🚀": "Entrepreneurship", "💡": "Entrepreneurship",
    "💻": "Technology", "⌨": "Technology", "🖥": "Technology", "🤖": "Technology",
    "📱": "Technology", "⚙": "Technology",
    "📷": "Photography", "📸": "Photography", "🎥": "Photography", "🎬": "Photography",
    "📹": "Photography",
    "🎵": "Music", "🎶": "Music", "🎤": "Music", "🎸": "Music", "🎹": "Music",
    "🥁": "Music", "🎧": "Music",
    "🎮": "Gaming", "🕹": "Gaming", "👾": "Gaming",
    "📚": "Education", "🎓": "Education", "✏": "Education", "📝": "Education",
    "💰": "Finance", "💵": "Finance", "📊": "Finance", "🪙": "Finance", "₿": "Finance",
    "🐶": "Pets", "🐱": "Pets", "🐾": "Pets", "🐕": "Pets", "🐈": "Pets", "🦜": "Pets",
    "🚗": "Cars", "🏎": "Cars", "🚘": "Cars", "🏍": "Cars", "🛵": "Cars", "🔧": "Cars",
    "🎨": "Art", "🖌": "Art", "🖼": "Art", "✍": "Art",
    "🌱": "Lifestyle", "🌟": "Lifestyle", "✨": "Lifestyle",
    "👶": "Parenting", "🍼": "Parenting", "👨‍👩‍👧": "Parenting",
    "🩺": "Health", "⚕": "Health", "🏥": "Health", "💊": "Health",
    "💎": "Luxury", "👑": "Luxury", "🛥": "Luxury", "🥂": "Luxury",
    "🏠": "Real Estate", "🏡": "Real Estate", "🏗": "Real Estate", "🔑": "Real Estate",
}

# ── Gazetteer: places ────────────────────────────────────────────────────
# Any hit here yields BOTH `loc:<Name>` and the Travel category — the location
# rule the pipeline is specified around. Canonical spelling is the value, so
# "uae" and "dubai" both come back titled correctly.
LOCATIONS: dict[str, str] = {}


def _places(*names: str) -> None:
    """Register places under their own lower-cased form."""
    for name in names:
        LOCATIONS[name.lower()] = name


# Pakistan — the set this pipeline runs against is Pakistan-heavy, and its
# northern destinations are exactly the ones the spec's examples name.
_places(
    "Pakistan", "Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad",
    "Multan", "Peshawar", "Quetta", "Sialkot", "Gujranwala", "Hyderabad",
    "Bahawalpur", "Sargodha", "Abbottabad", "Mardan", "Mirpur", "Sahiwal",
    "Gujrat", "Jhelum", "Okara", "Sukkur", "Larkana", "Chiniot", "Kasur",
    "Hunza", "Skardu", "Gilgit", "Naran", "Kaghan", "Swat", "Kalam", "Murree",
    "Chitral", "Kumrat", "Fairy Meadows", "Nathia Gali", "Neelum Valley",
    "Shogran", "Ratti Gali", "Deosai", "Attabad", "Khunjerab", "Baltistan",
    "Kashmir", "Azad Kashmir", "Balochistan", "Sindh", "Punjab", "Gwadar",
    "Thar", "Cholistan", "Malam Jabba", "Shandur", "Passu", "Karimabad",
)
# Countries
_places(
    "Afghanistan", "Albania", "Algeria", "Argentina", "Armenia", "Australia",
    "Austria", "Azerbaijan", "Bahrain", "Bangladesh", "Belgium", "Bhutan",
    "Brazil", "Bulgaria", "Cambodia", "Canada", "Chile", "China", "Colombia",
    "Croatia", "Cyprus", "Czechia", "Denmark", "Egypt", "Estonia", "Ethiopia",
    "Finland", "France", "Georgia", "Germany", "Ghana", "Greece", "Hungary",
    "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland", "Israel",
    "Italy", "Japan", "Jordan", "Kazakhstan", "Kenya", "Kuwait", "Kyrgyzstan",
    "Latvia", "Lebanon", "Libya", "Lithuania", "Luxembourg", "Malaysia",
    "Maldives", "Malta", "Mexico", "Mongolia", "Montenegro", "Morocco",
    "Myanmar", "Nepal", "Netherlands", "New Zealand", "Nigeria", "Norway",
    "Oman", "Peru", "Philippines", "Poland", "Portugal", "Qatar", "Romania",
    "Russia", "Rwanda", "Saudi Arabia", "Scotland", "Senegal", "Serbia",
    "Singapore", "Slovakia", "Slovenia", "Somalia", "South Africa",
    "South Korea", "Spain", "Sri Lanka", "Sudan", "Sweden", "Switzerland",
    "Syria", "Taiwan", "Tajikistan", "Tanzania", "Thailand", "Tunisia",
    "Turkey", "Turkiye", "Uganda", "Ukraine", "United Kingdom", "United States",
    "Uruguay", "Uzbekistan", "Vietnam", "Wales", "Yemen", "Zimbabwe",
    "England", "Ireland", "Palestine",
)
# Cities and destinations that carry travel meaning on their own.
_places(
    "Dubai", "Abu Dhabi", "Sharjah", "Doha", "Riyadh", "Jeddah", "Mecca",
    "Medina", "Istanbul", "Ankara", "Antalya", "Cappadocia", "Bodrum",
    "London", "Manchester", "Liverpool", "Birmingham", "Edinburgh", "Glasgow",
    "Paris", "Nice", "Lyon", "Marseille", "Bordeaux",
    "Rome", "Milan", "Venice", "Florence", "Naples", "Sicily", "Amalfi",
    "Madrid", "Barcelona", "Seville", "Valencia", "Ibiza", "Mallorca",
    "Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne",
    "Amsterdam", "Rotterdam", "Brussels", "Vienna", "Prague", "Budapest",
    "Zurich", "Geneva", "Interlaken", "Lucerne", "Zermatt",
    "Lisbon", "Porto", "Athens", "Santorini", "Mykonos", "Crete",
    "Oslo", "Stockholm", "Copenhagen", "Helsinki", "Reykjavik",
    "Moscow", "Baku", "Tbilisi", "Yerevan", "Almaty", "Tashkent", "Samarkand",
    "New York", "Los Angeles", "San Francisco", "Chicago", "Miami", "Las Vegas",
    "Boston", "Seattle", "Austin", "Houston", "Toronto", "Vancouver", "Montreal",
    "Mexico City", "Cancun", "Rio de Janeiro", "Sao Paulo", "Buenos Aires",
    "Lima", "Bogota", "Santiago", "Cusco", "Patagonia",
    "Tokyo", "Osaka", "Kyoto", "Seoul", "Busan", "Beijing", "Shanghai",
    "Hong Kong", "Taipei", "Bangkok", "Phuket", "Chiang Mai", "Krabi",
    "Bali", "Jakarta", "Lombok", "Singapore", "Kuala Lumpur", "Langkawi",
    "Manila", "Cebu", "Boracay", "Hanoi", "Ho Chi Minh", "Da Nang",
    "Delhi", "New Delhi", "Mumbai", "Bangalore", "Kolkata", "Chennai",
    "Hyderabad", "Jaipur", "Goa", "Kerala", "Ladakh", "Manali", "Rishikesh",
    "Kathmandu", "Pokhara", "Everest", "Annapurna", "Colombo", "Male",
    "Cairo", "Marrakech", "Casablanca", "Cape Town", "Johannesburg",
    "Nairobi", "Zanzibar", "Serengeti", "Kilimanjaro", "Victoria Falls",
    "Sydney", "Melbourne", "Brisbane", "Perth", "Auckland", "Queenstown",
    "Baghdad", "Tehran", "Amman", "Petra", "Beirut", "Damascus", "Muscat",
    "Kabul", "Dhaka", "Chittagong", "Thimphu",
    "Swiss Alps", "The Alps", "Sahara", "Amazon", "Bora Bora", "Fiji",
    "Seychelles", "Mauritius", "Zanzibar", "Tuscany", "Provence", "Bavaria",
)

# ── Sports, teams, athletes ──────────────────────────────────────────────
SPORTS: dict[str, str] = {
    "football": "Football", "soccer": "Football", "futbol": "Football",
    "cricket": "Cricket", "basketball": "Basketball", "tennis": "Tennis",
    "badminton": "Badminton", "volleyball": "Volleyball", "hockey": "Hockey",
    "field hockey": "Hockey", "ice hockey": "Ice Hockey", "baseball": "Baseball",
    "rugby": "Rugby", "golf": "Golf", "boxing": "Boxing", "mma": "MMA",
    "ufc": "MMA", "wrestling": "Wrestling", "kabaddi": "Kabaddi",
    "swimming": "Swimming", "athletics": "Athletics", "running": "Running",
    "marathon": "Running", "cycling": "Cycling", "skiing": "Skiing",
    "snowboarding": "Snowboarding", "surfing": "Surfing", "skateboarding": "Skateboarding",
    "climbing": "Climbing", "rock climbing": "Climbing", "archery": "Archery",
    "shooting": "Shooting", "fencing": "Fencing", "gymnastics": "Gymnastics",
    "karate": "Karate", "taekwondo": "Taekwondo", "judo": "Judo",
    "muay thai": "Muay Thai", "jiu jitsu": "Jiu-Jitsu", "bjj": "Jiu-Jitsu",
    "squash": "Squash", "table tennis": "Table Tennis", "polo": "Polo",
    "formula 1": "Formula 1", "f1": "Formula 1", "motogp": "MotoGP",
    "horse riding": "Equestrian", "equestrian": "Equestrian", "padel": "Padel",
    "snooker": "Snooker", "billiards": "Billiards", "darts": "Darts",
    "esports": "Esports", "chess": "Chess",
}

TEAMS: dict[str, str] = {
    "real madrid": "Real Madrid", "barcelona fc": "FC Barcelona", "fc barcelona": "FC Barcelona",
    "manchester united": "Manchester United", "man utd": "Manchester United",
    "man united": "Manchester United", "manchester city": "Manchester City",
    "man city": "Manchester City", "liverpool fc": "Liverpool FC",
    "chelsea fc": "Chelsea FC", "arsenal fc": "Arsenal FC", "tottenham": "Tottenham Hotspur",
    "juventus": "Juventus", "ac milan": "AC Milan", "inter milan": "Inter Milan",
    "bayern munich": "Bayern Munich", "borussia dortmund": "Borussia Dortmund",
    "psg": "Paris Saint-Germain", "paris saint germain": "Paris Saint-Germain",
    "atletico madrid": "Atletico Madrid", "napoli": "SSC Napoli", "as roma": "AS Roma",
    "ajax": "AFC Ajax", "benfica": "SL Benfica", "porto fc": "FC Porto",
    "lakers": "Los Angeles Lakers", "golden state warriors": "Golden State Warriors",
    "chicago bulls": "Chicago Bulls", "boston celtics": "Boston Celtics",
    "miami heat": "Miami Heat",
    "pakistan cricket": "Pakistan Cricket Team", "team pakistan": "Pakistan Cricket Team",
    "karachi kings": "Karachi Kings", "lahore qalandars": "Lahore Qalandars",
    "peshawar zalmi": "Peshawar Zalmi", "islamabad united": "Islamabad United",
    "multan sultans": "Multan Sultans", "quetta gladiators": "Quetta Gladiators",
    "mumbai indians": "Mumbai Indians", "chennai super kings": "Chennai Super Kings",
    "royal challengers": "Royal Challengers Bangalore",
}

ATHLETES: dict[str, str] = {
    "messi": "Lionel Messi", "lionel messi": "Lionel Messi",
    "ronaldo": "Cristiano Ronaldo", "cristiano ronaldo": "Cristiano Ronaldo",
    "neymar": "Neymar", "mbappe": "Kylian Mbappe", "haaland": "Erling Haaland",
    "babar azam": "Babar Azam", "shaheen afridi": "Shaheen Afridi",
    "shahid afridi": "Shahid Afridi", "virat kohli": "Virat Kohli",
    "ms dhoni": "MS Dhoni", "lebron james": "LeBron James", "kobe bryant": "Kobe Bryant",
    "michael jordan": "Michael Jordan", "roger federer": "Roger Federer",
    "rafael nadal": "Rafael Nadal", "novak djokovic": "Novak Djokovic",
    "conor mcgregor": "Conor McGregor", "khabib": "Khabib Nurmagomedov",
    "lewis hamilton": "Lewis Hamilton", "max verstappen": "Max Verstappen",
}

# ── Brands, companies, universities ──────────────────────────────────────
BRANDS: dict[str, str] = {
    "nike": "Nike", "adidas": "Adidas", "puma": "Puma", "reebok": "Reebok",
    "under armour": "Under Armour", "gucci": "Gucci", "prada": "Prada",
    "chanel": "Chanel", "louis vuitton": "Louis Vuitton", "versace": "Versace",
    "dior": "Dior", "balenciaga": "Balenciaga", "rolex": "Rolex", "omega watches": "Omega",
    "apple": "Apple", "samsung": "Samsung", "google": "Google", "microsoft": "Microsoft",
    "amazon": "Amazon", "meta": "Meta", "tesla": "Tesla", "netflix": "Netflix",
    "bmw": "BMW", "mercedes": "Mercedes-Benz", "audi": "Audi", "toyota": "Toyota",
    "honda": "Honda", "suzuki": "Suzuki", "hyundai": "Hyundai", "kia": "Kia",
    "porsche": "Porsche", "ferrari": "Ferrari", "lamborghini": "Lamborghini",
    "ford": "Ford", "nissan": "Nissan", "volkswagen": "Volkswagen", "yamaha": "Yamaha",
    "kawasaki": "Kawasaki", "ducati": "Ducati", "harley davidson": "Harley-Davidson",
    "starbucks": "Starbucks", "mcdonalds": "McDonald's", "kfc": "KFC",
    "coca cola": "Coca-Cola", "pepsi": "Pepsi", "nestle": "Nestle",
    "canon": "Canon", "nikon": "Nikon", "sony": "Sony", "gopro": "GoPro", "dji": "DJI",
    "adobe": "Adobe", "shopify": "Shopify", "daraz": "Daraz", "foodpanda": "Foodpanda",
    "careem": "Careem", "uber": "Uber", "emirates": "Emirates", "qatar airways": "Qatar Airways",
    "pia": "PIA", "turkish airlines": "Turkish Airlines",
}

UNIVERSITIES: dict[str, str] = {
    "harvard": "Harvard University", "mit": "MIT", "stanford": "Stanford University",
    "oxford": "University of Oxford", "cambridge": "University of Cambridge",
    "yale": "Yale University", "princeton": "Princeton University",
    "berkeley": "UC Berkeley", "caltech": "Caltech", "imperial college": "Imperial College London",
    "lums": "LUMS", "nust": "NUST", "fast nuces": "FAST-NUCES", "comsats": "COMSATS",
    "uet": "UET", "punjab university": "University of the Punjab",
    "karachi university": "University of Karachi", "iba": "IBA Karachi",
    "giki": "GIKI", "air university": "Air University", "bahria university": "Bahria University",
    "iit": "IIT", "nit": "NIT", "delhi university": "University of Delhi",
}

# ── Occupations and hobbies ──────────────────────────────────────────────
OCCUPATIONS: dict[str, str] = {
    "engineer": "Engineer", "software engineer": "Software Engineer",
    "doctor": "Doctor", "surgeon": "Surgeon", "dentist": "Dentist", "nurse": "Nurse",
    "teacher": "Teacher", "professor": "Professor", "lecturer": "Lecturer",
    "lawyer": "Lawyer", "advocate": "Advocate", "accountant": "Accountant",
    "architect": "Architect", "pilot": "Pilot", "journalist": "Journalist",
    "chef": "Chef", "barista": "Barista", "photographer": "Photographer",
    "videographer": "Videographer", "designer": "Designer",
    "graphic designer": "Graphic Designer", "developer": "Developer",
    "freelancer": "Freelancer", "consultant": "Consultant", "realtor": "Realtor",
    "personal trainer": "Personal Trainer", "makeup artist": "Makeup Artist",
    "hairstylist": "Hairstylist", "barber": "Barber", "tattoo artist": "Tattoo Artist",
    "dj": "DJ", "producer": "Producer", "model": "Model", "actor": "Actor",
    "actress": "Actress", "writer": "Writer", "author": "Author", "poet": "Poet",
    "psychologist": "Psychologist", "pharmacist": "Pharmacist", "scientist": "Scientist",
    "researcher": "Researcher", "student": "Student", "entrepreneur": "Entrepreneur",
    "ceo": "CEO", "founder": "Founder", "influencer": "Influencer",
    "content creator": "Content Creator", "youtuber": "YouTuber", "blogger": "Blogger",
}

HOBBIES: dict[str, str] = {
    "reading": "Reading", "writing": "Writing", "poetry": "Poetry",
    "gardening": "Gardening", "cooking": "Cooking", "baking": "Baking",
    "painting": "Painting", "drawing": "Drawing", "sketching": "Sketching",
    "calligraphy": "Calligraphy", "knitting": "Knitting", "crochet": "Crochet",
    "photography": "Photography", "birdwatching": "Birdwatching",
    "fishing": "Fishing", "hunting": "Hunting", "camping": "Camping",
    "hiking": "Hiking", "trekking": "Trekking", "cycling": "Cycling",
    "dancing": "Dancing", "singing": "Singing", "gaming": "Gaming",
    "chess": "Chess", "collecting": "Collecting", "travelling": "Travelling",
    "meditation": "Meditation", "yoga": "Yoga", "coding": "Coding",
    "astronomy": "Astronomy", "stargazing": "Stargazing", "podcasting": "Podcasting",
}

EVENTS: dict[str, str] = {
    "olympics": "Olympics", "world cup": "World Cup", "fifa world cup": "FIFA World Cup",
    "champions league": "UEFA Champions League", "premier league": "Premier League",
    "la liga": "La Liga", "serie a": "Serie A", "bundesliga": "Bundesliga",
    "psl": "Pakistan Super League", "ipl": "Indian Premier League",
    "wimbledon": "Wimbledon", "us open": "US Open", "super bowl": "Super Bowl",
    "ramadan": "Ramadan", "eid": "Eid", "hajj": "Hajj", "umrah": "Umrah",
    "coachella": "Coachella", "met gala": "Met Gala", "comic con": "Comic Con",
}

RESTAURANTS: dict[str, str] = {
    "kfc": "KFC", "mcdonalds": "McDonald's", "subway": "Subway",
    "dominos": "Domino's", "pizza hut": "Pizza Hut", "hardees": "Hardee's",
    "burger king": "Burger King", "nandos": "Nando's", "starbucks": "Starbucks",
    "tim hortons": "Tim Hortons", "dunkin": "Dunkin'",
}

# Which facet implies which category. This is where `loc:` earns the Travel tag
# the spec requires, and a named sport earns Sports.
FACET_CATEGORY: dict[str, str] = {
    "loc": "Travel",
    "sport": "Sports",
    "team": "Sports",
    "athlete": "Sports",
    "university": "Education",
    "restaurant": "Food",
}

# Facets whose tag is written `facet:Value`; everything else is a bare tag.
PREFIXED_FACETS = ("loc", "sport", "team", "athlete", "brand", "org",
                   "university", "job", "hobby", "event", "restaurant")
