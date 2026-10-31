"""Extract visual subjects and search keywords from scene text (rule-based NLP)."""

from __future__ import annotations

import re
from collections import Counter

from video_maker import clean_text, split_scenes

STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "so", "as", "at", "by",
    "for", "from", "in", "into", "of", "on", "onto", "to", "with", "without",
    "about", "above", "after", "again", "against", "all", "am", "are", "be",
    "been", "being", "before", "below", "between", "both", "can", "could",
    "did", "do", "does", "doing", "down", "during", "each", "few", "further",
    "had", "has", "have", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "is", "it", "its", "itself", "just",
    "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "off",
    "once", "only", "other", "our", "ours", "ourselves", "out", "over", "own",
    "same", "she", "should", "some", "such", "than", "that", "their", "theirs",
    "them", "themselves", "there", "these", "they", "this", "those", "through",
    "too", "under", "until", "up", "very", "was", "we", "were", "what", "when",
    "where", "which", "while", "who", "whom", "why", "will", "would", "you",
    "your", "yours", "yourself", "yourselves", "also", "still", "already",
    "almost", "always", "never", "often", "maybe", "perhaps", "really",
    "quite", "rather", "something", "someone", "everything", "everyone",
    "anything", "anyone", "nothing", "nobody", "knows", "know", "way",
    "home", "made", "make", "makes", "gets", "get", "got", "goes", "go",
    "went", "come", "comes", "came", "leave", "leaves", "left", "pass",
    "passes", "passed", "reach", "reaches", "reached", "find", "finds",
    "found", "see", "sees", "saw", "look", "looks", "looking", "say",
    "says", "said", "tell", "tells", "told", "want", "wants", "wanted",
    "like", "likes", "liked", "seem", "seems", "become", "becomes", "became",
}

VISUAL_HINTS = {
    "sunrise": "sunrise sky",
    "sunset": "sunset sky",
    "dawn": "dawn light",
    "night": "night city",
    "harbor": "harbor boats",
    "lighthouse": "lighthouse coast",
    "whale": "whale ocean",
    "boat": "boat water",
    "paper": "paper craft",
    "forest": "forest trees",
    "ocean": "ocean waves",
    "sea": "sea water",
    "mountain": "mountain landscape",
    "city": "city skyline",
    "street": "city street",
    "kitchen": "kitchen interior",
    "fox": "red fox",
    "letter": "handwritten letter",
    "lantern": "lantern light",
    "rain": "rain weather",
    "snow": "snow winter",
    "desert": "desert sand",
    "beach": "beach sand",
    "river": "river water",
    "bridge": "bridge architecture",
    "train": "train railway",
    "plane": "airplane sky",
    "car": "car road",
    "bike": "bicycle street",
    "dog": "dog animal",
    "cat": "cat animal",
    "bird": "bird wildlife",
    "flower": "flower garden",
    "tree": "tree nature",
    "coffee": "coffee cup",
    "office": "office desk",
    "school": "school classroom",
    "hospital": "hospital building",
    "market": "market stall",
    "farm": "farm field",
    "people": "people crowd",
    "child": "child portrait",
    "woman": "woman portrait",
    "man": "man portrait",
    "family": "family outdoors",
    "food": "food table",
    "music": "music concert",
    "tech": "technology workspace",
    "business": "business meeting",
    "travel": "travel destination",
    "smoke": "cigarette smoke",
    "drink": "drink glass",
    "chess": "chess board",
    "player": "chess player",
    "genius": "genius portrait",
    "wine": "wine glass",
    "cigar": "cigar smoke",
    "trophy": "trophy award",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'-]{1,}", text.lower())


def extract_subjects(text: str, limit: int = 8) -> list[str]:
    """Pull visual subjects / noun-like phrases for short-script expansion."""
    tokens = [token for token in _tokens(text) if token not in STOPWORDS and len(token) > 2]
    if not tokens:
        return ["cinematic landscape"]

    subjects: list[str] = []
    # Prefer known visual nouns and adjacent pairs ("red fox", "paper boat").
    for index, word in enumerate(tokens):
        if word in VISUAL_HINTS:
            phrase = VISUAL_HINTS[word]
            if phrase not in subjects:
                subjects.append(phrase)
        if index + 1 < len(tokens):
            pair = f"{word} {tokens[index + 1]}"
            if word in VISUAL_HINTS or tokens[index + 1] in VISUAL_HINTS:
                if pair not in subjects:
                    subjects.append(pair)
        if len(subjects) >= limit:
            break

    if len(subjects) < 2:
        ranked = [word for word, _ in Counter(tokens).most_common(limit)]
        for word in ranked:
            phrase = VISUAL_HINTS.get(word, word)
            if phrase not in subjects:
                subjects.append(phrase)
            if len(subjects) >= limit:
                break
    return subjects[:limit] or ["cinematic landscape"]


def extract_keywords(scene_text: str, limit: int = 5) -> list[str]:
    """Return short visual search terms for one scene."""
    cleaned = clean_text(scene_text)
    tokens = [token for token in _tokens(cleaned) if token not in STOPWORDS and len(token) > 2]
    if not tokens:
        return ["cinematic landscape"]

    counts = Counter(tokens)
    ranked = [word for word, _ in counts.most_common(12)]
    phrases: list[str] = []

    for word in ranked:
        mapped = VISUAL_HINTS.get(word)
        if mapped and mapped not in phrases:
            phrases.append(mapped)
        if len(phrases) >= 2:
            break

    if len(ranked) >= 2:
        combo = f"{ranked[0]} {ranked[1]}"
        if combo not in phrases:
            phrases.insert(0 if not phrases else 1, combo)

    for word in ranked:
        if word not in phrases:
            phrases.append(word)
        if len(phrases) >= limit:
            break
    return phrases[:limit] or ["cinematic landscape"]


def scene_plan(text: str) -> list[dict]:
    """Split text into scenes and attach keyword lists."""
    scenes = split_scenes(text)
    planned = []
    for index, scene in enumerate(scenes):
        words = extract_keywords(scene)
        planned.append(
            {
                "index": index,
                "text": scene,
                "keywords": words,
                "query": words[0],
            }
        )
    return planned
