import os
import io
import json
import time
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from google import genai
from google.genai import types

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload


# ============================================================
# CONFIGURATION
# ============================================================

# Keep the model name here so it can be changed easily later.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# Automatic fallback models if the primary model is unavailable.
GEMINI_FALLBACK_MODELS = [
    GEMINI_MODEL,
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]

MAX_VIDEOS_PER_RUN = int(os.getenv("MAX_VIDEOS_PER_RUN", "3"))

MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", "500"))

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".avi",
    ".mkv",
}

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

INDIA_REGION = "IN"
INDIA_LANGUAGE = "en"


# ============================================================
# ENVIRONMENT / SECRETS
# ============================================================

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]

DRIVE_REFRESH_TOKEN = os.environ["GOOGLE_DRIVE_REFRESH_TOKEN"]
GOOGLE_OAUTH_CLIENT_ID = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
GOOGLE_OAUTH_CLIENT_SECRET = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]

DRIVE_INPUT_FOLDER_ID = os.environ["DRIVE_INPUT_FOLDER_ID"]
DRIVE_METADATA_FOLDER_ID = os.environ["DRIVE_METADATA_FOLDER_ID"]
DRIVE_FAILED_FOLDER_ID = os.environ["DRIVE_FAILED_FOLDER_ID"]
DRIVE_LOGS_FOLDER_ID = os.environ["DRIVE_LOGS_FOLDER_ID"]


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("youtube-shorts-code1")


# ============================================================
# DIRECTORIES
# ============================================================

WORK_DIR = Path("work")
WORK_DIR.mkdir(exist_ok=True)


# ============================================================
# GOOGLE DRIVE
# ============================================================

def get_drive_service():
    """
    Create an authenticated Google Drive client using
    the user's OAuth refresh token.
    """

    credentials = Credentials(
        token=None,
        refresh_token=DRIVE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=[DRIVE_SCOPE],
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


def list_input_videos(drive_service):
    """
    Find videos inside 01_INPUT.
    """

    query = (
        f"'{DRIVE_INPUT_FOLDER_ID}' in parents "
        f"and trashed = false"
    )

    response = (
        drive_service.files()
        .list(
            q=query,
            pageSize=100,
            orderBy="createdTime",
            fields="files(id,name,mimeType,size,createdTime,modifiedTime)",
        )
        .execute()
    )

    files = response.get("files", [])

    videos = []

    for file in files:
        name = file.get("name", "")
        suffix = Path(name).suffix.lower()

        if suffix in VIDEO_EXTENSIONS:
            videos.append(file)

    return videos


def metadata_exists(drive_service, video_name):
    """
    Check whether a matching TXT file already exists.
    """

    metadata_name = Path(video_name).stem + ".txt"

    query = (
        f"'{DRIVE_METADATA_FOLDER_ID}' in parents "
        f"and name = '{metadata_name.replace(chr(39), chr(39) + chr(39))}' "
        f"and trashed = false"
    )

    response = (
        drive_service.files()
        .list(
            q=query,
            pageSize=10,
            fields="files(id,name)",
        )
        .execute()
    )

    return len(response.get("files", [])) > 0


def download_drive_file(drive_service, file_id, destination):
    """
    Download a Drive file to the GitHub runner.
    """

    request = drive_service.files().get_media(fileId=file_id)

    with open(destination, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)

        done = False

        while not done:
            status, done = downloader.next_chunk()

            if status:
                logger.info(
                    "Download progress: %.1f%%",
                    status.progress() * 100,
                )


def upload_text_file(drive_service, file_path, folder_id):
    """
    Upload a TXT file to Drive.
    """

    metadata = {
        "name": file_path.name,
        "parents": [folder_id],
        "mimeType": "text/plain",
    }

    media = MediaFileUpload(
        str(file_path),
        mimetype="text/plain",
        resumable=False,
    )

    result = (
        drive_service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name",
        )
        .execute()
    )

    return result


def upload_log(drive_service, text):
    """
    Save a log file in 05_LOGS.
    """

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%d_%H%M%S"
    )

    log_path = WORK_DIR / f"code1_{timestamp}.log"

    log_path.write_text(
        text,
        encoding="utf-8",
    )

    try:
        upload_text_file(
            drive_service,
            log_path,
            DRIVE_LOGS_FOLDER_ID,
        )
    except Exception as exc:
        logger.warning(
            "Could not upload log: %s",
            exc,
        )


def move_file_to_failed(drive_service, file_id):
    """
    Move a failed video from 01_INPUT to 04_FAILED.
    """

    drive_service.files().update(
        fileId=file_id,
        addParents=DRIVE_FAILED_FOLDER_ID,
        removeParents=DRIVE_INPUT_FOLDER_ID,
        fields="id,parents",
    ).execute()


# ============================================================
# GEMINI
# ============================================================

def get_gemini_client():
    return genai.Client(
        api_key=GEMINI_API_KEY
    )


def upload_video_to_gemini(client, video_path):
    """
    Upload the video to Gemini Files API.
    """

    logger.info(
        "Uploading video to Gemini: %s",
        video_path.name,
    )

    uploaded = client.files.upload(
        file=str(video_path)
    )

    start = time.time()

    while True:

        if uploaded.state:
            state_name = uploaded.state.name

            logger.info(
                "Gemini video state: %s",
                state_name,
            )

            if state_name == "ACTIVE":
                return uploaded

            if state_name in {
                "FAILED",
                "ERROR",
            }:
                raise RuntimeError(
                    f"Gemini video processing failed: {state_name}"
                )

        if time.time() - start > 600:
            raise TimeoutError(
                "Gemini video processing timed out."
            )

        time.sleep(5)

        uploaded = client.files.get(
            name=uploaded.name
        )


# ============================================================
# VIDEO ANALYSIS
# ============================================================

def analyze_video(client, uploaded_file):
    """
    Understand the Short.

    We intentionally request plain JSON-like labeled information
    instead of a complicated nested schema.
    """

    prompt = """
You are analyzing a YouTube Short for metadata generation.

Watch and understand the ENTIRE video.

Identify:

1. What is happening in the video?
2. Main subject/person/object/topic.
3. Important spoken words or narration if understandable.
4. Main message or story.
5. Key visual elements.
6. Location/country if clearly identifiable.
7. Names of people/brands/places only when reasonably certain.
8. Likely audience.
9. Important keywords.
10. Whether the content is factual, entertainment, educational,
    news/current affairs, commentary, reaction, sports, technology,
    comedy, lifestyle, etc.

Do not invent facts.

Return ONLY this format:

SUMMARY:
...

TOPIC:
...

CONTENT_TYPE:
...

KEYWORDS:
keyword1, keyword2, keyword3, keyword4, keyword5

IMPORTANT_NAMES:
...

LOCATION:
...

AUDIENCE:
...

CONFIDENCE:
0-10
"""

    for model in GEMINI_FALLBACK_MODELS:

        try:
            logger.info(
                "Analyzing video with Gemini model: %s",
                model,
            )

            response = client.models.generate_content(
                model=model,
                contents=[
                    uploaded_file,
                    prompt,
                ],
            )

            text = response.text.strip()

            if text:
                return text

        except Exception as exc:
            logger.warning(
                "Video analysis failed with %s: %s",
                model,
                exc,
            )

    raise RuntimeError(
        "All Gemini video-analysis models failed."
    )


# ============================================================
# YOUTUBE RESEARCH
# ============================================================

def youtube_search(query):
    """
    Search recent YouTube videos related to the topic.
    """

    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": "relevance",
        "publishedAfter": (
            datetime.now(timezone.utc)
            - timedelta(days=30)
        ).isoformat().replace("+00:00", "Z"),
        "regionCode": INDIA_REGION,
        "relevanceLanguage": INDIA_LANGUAGE,
        "maxResults": 5,
        "safeSearch": "moderate",
        "key": YOUTUBE_API_KEY,
    }

    response = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    results = []

    for item in data.get("items", []):

        snippet = item.get("snippet", {})

        results.append(
            {
                "title": snippet.get("title", ""),
                "description": snippet.get("description", "")[
                    :500
                ],
                "published_at": snippet.get(
                    "publishedAt",
                    "",
                ),
                "channel": snippet.get(
                    "channelTitle",
                    "",
                ),
            }
        )

    return results


def collect_youtube_research(analysis_text):
    """
    Use the topic/keywords from the video analysis
    to collect recent YouTube signals.
    """

    topic = extract_field(
        analysis_text,
        "TOPIC",
    )

    keywords = extract_field(
        analysis_text,
        "KEYWORDS",
    )

    searches = []

    if topic:
        searches.append(topic)

    if keywords:
        first_keywords = [
            x.strip()
            for x in keywords.split(",")
            if x.strip()
        ]

        if first_keywords:
            searches.append(
                " ".join(first_keywords[:3])
            )

    searches = searches[:2]

    all_results = []

    for query in searches:

        try:
            logger.info(
                "YouTube research query: %s",
                query,
            )

            results = youtube_search(query)

            all_results.extend(results)

        except Exception as exc:

            logger.warning(
                "YouTube research failed: %s",
                exc,
            )

    return all_results


# ============================================================
# WEB + TREND RESEARCH
# ============================================================

def web_trend_research(client, analysis_text):
    """
    Gemini uses Google Search grounding to research current
    web information.

    This replaces the older Custom Search API approach.
    """

    prompt = f"""
You are doing current web research for a YouTube Short.

Use Google Search to find CURRENT and RELEVANT information.

Do NOT force trends into the content.

A trend is useful only if it is genuinely connected to
the Short's subject.

VIDEO ANALYSIS:
{analysis_text}

Research:

1. Recent developments related to this topic.
2. Current news or public interest surrounding it.
3. Current terminology/phrases people are using.
4. Recent events that genuinely relate to the topic.
5. Whether there is a useful current angle for this Short.
6. Any important factual correction needed.

Prefer reliable and recent sources.

Return concise findings.

At the end return:

TREND_RELEVANCE:
0-10

CURRENT_ANGLE:
...

IMPORTANT_FACT_CHECK:
...

RESEARCH_SUMMARY:
...
"""

    for model in GEMINI_FALLBACK_MODELS:

        try:

            logger.info(
                "Running Google Search grounding with %s",
                model,
            )

            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[
                        types.Tool(
                            google_search=types.GoogleSearch()
                        )
                    ]
                ),
            )

            if response.text:
                return response.text.strip()

        except Exception as exc:

            logger.warning(
                "Web research failed with %s: %s",
                model,
                exc,
            )

    return (
        "TREND_RELEVANCE:\n"
        "5\n\n"
        "CURRENT_ANGLE:\n"
        "No reliable current angle found.\n\n"
        "IMPORTANT_FACT_CHECK:\n"
        "No additional fact check available.\n\n"
        "RESEARCH_SUMMARY:\n"
        "Current web research was unavailable."
    )


# ============================================================
# METADATA GENERATION
# ============================================================

def generate_metadata(
    client,
    analysis_text,
    youtube_results,
    web_research,
):
    """
    Generate the final Shorts metadata.
    """

    youtube_context = "\n".join(
        [
            (
                f"- {item['title']} | "
                f"{item['channel']} | "
                f"{item['published_at']}"
            )
            for item in youtube_results[:10]
        ]
    )

    if not youtube_context:
        youtube_context = (
            "No useful YouTube search results were found."
        )

    prompt = f"""
You are an expert YouTube Shorts metadata strategist.

Create metadata ONLY for the actual video content.

IMPORTANT RULES:

- Never invent what happens in the video.
- Never use an unrelated trend just because it is popular.
- Current trends are allowed only when genuinely relevant.
- Prefer curiosity, clarity and accuracy.
- Do not use misleading clickbait.
- Do not copy another video's title.
- Do not overstuff keywords.
- Use natural language.
- Titles should be suitable for YouTube Shorts.
- Keep titles reasonably short.
- Generate exactly 3 title options.
- Select exactly ONE recommended title.
- Generate 5 to 8 relevant hashtags.
- Hashtags must actually relate to the video.
- Do not use #Shorts unless it naturally fits.
- Do not use unrelated celebrity/news hashtags.
- Description should be concise and useful.
- Do not repeat the same sentence several times.
- Do not claim something happened if it isn't supported by the video.

VIDEO ANALYSIS:
{analysis_text}

RECENT YOUTUBE RESULTS:
{youtube_context}

CURRENT WEB RESEARCH:
{web_research}

Return EXACTLY this format:

TITLE_1:
...

TITLE_2:
...

TITLE_3:
...

RECOMMENDED_TITLE:
...

DESCRIPTION:
...

HASHTAGS:
#tag1 #tag2 #tag3 #tag4 #tag5

CONTENT_SCORE:
0-10

TREND_SCORE:
0-10

RELEVANCE_DECISION:
USE_TREND
or
NO_TREND

RELEVANCE_REASON:
...

TREND_ANGLE:
...

KEYWORDS:
keyword1, keyword2, keyword3, keyword4, keyword5
"""

    for model in GEMINI_FALLBACK_MODELS:

        try:

            logger.info(
                "Generating metadata with %s",
                model,
            )

            response = client.models.generate_content(
                model=model,
                contents=prompt,
            )

            text = response.text.strip()

            if text:
                return text

        except Exception as exc:

            logger.warning(
                "Metadata generation failed with %s: %s",
                model,
                exc,
            )

    raise RuntimeError(
        "All Gemini metadata-generation models failed."
    )


# ============================================================
# FIELD EXTRACTION
# ============================================================

def extract_field(text, field_name):
    """
    Extract a labeled field from Gemini output.
    """

    marker = field_name + ":"

    start = text.find(marker)

    if start == -1:
        return ""

    start += len(marker)

    remaining = text[start:]

    lines = remaining.splitlines()

    values = []

    for line in lines:

        stripped = line.strip()

        if not stripped:
            if values:
                break
            continue

        # Stop when another uppercase field begins.
        if (
            ":" in stripped
            and stripped.split(":", 1)[0]
            .strip()
            .replace("_", "")
            .isalnum()
            and stripped.split(":", 1)[0].isupper()
        ):
            break

        values.append(stripped)

    return "\n".join(values).strip()


# ============================================================
# METADATA VALIDATION
# ============================================================

def validate_metadata(metadata_text):
    """
    Basic quality gate before saving the TXT.
    """

    required_fields = [
        "TITLE_1:",
        "TITLE_2:",
        "TITLE_3:",
        "RECOMMENDED_TITLE:",
        "DESCRIPTION:",
        "HASHTAGS:",
        "CONTENT_SCORE:",
        "TREND_SCORE:",
        "RELEVANCE_DECISION:",
    ]

    missing = [
        field
        for field in required_fields
        if field not in metadata_text
    ]

    if missing:
        raise ValueError(
            "Metadata missing fields: "
            + ", ".join(missing)
        )

    recommended = extract_field(
        metadata_text,
        "RECOMMENDED_TITLE",
    )

    if not recommended:
        raise ValueError(
            "Recommended title is empty."
        )

    description = extract_field(
        metadata_text,
        "DESCRIPTION",
    )

    if not description:
        raise ValueError(
            "Description is empty."
        )

    hashtags = extract_field(
        metadata_text,
        "HASHTAGS",
    )

    hashtag_list = [
        item
        for item in hashtags.split()
        if item.startswith("#")
    ]

    if not hashtag_list:
        raise ValueError(
            "No hashtags generated."
        )

    if len(hashtag_list) > 15:
        raise ValueError(
            "Too many hashtags generated."
        )


# ============================================================
# BUILD FINAL TXT
# ============================================================

def build_metadata_file(
    video_name,
    analysis,
    web_research,
    metadata,
):
    """
    Build the human-readable TXT that Code 2 will later use.
    """

    title1 = extract_field(
        metadata,
        "TITLE_1",
    )

    title2 = extract_field(
        metadata,
        "TITLE_2",
    )

    title3 = extract_field(
        metadata,
        "TITLE_3",
    )

    recommended = extract_field(
        metadata,
        "RECOMMENDED_TITLE",
    )

    description = extract_field(
        metadata,
        "DESCRIPTION",
    )

    hashtags = extract_field(
        metadata,
        "HASHTAGS",
    )

    content_score = extract_field(
        metadata,
        "CONTENT_SCORE",
    )

    trend_score = extract_field(
        metadata,
        "TREND_SCORE",
    )

    relevance_decision = extract_field(
        metadata,
        "RELEVANCE_DECISION",
    )

    relevance_reason = extract_field(
        metadata,
        "RELEVANCE_REASON",
    )

    trend_angle = extract_field(
        metadata,
        "TREND_ANGLE",
    )

    keywords = extract_field(
        metadata,
        "KEYWORDS",
    )

    video_topic = extract_field(
        analysis,
        "TOPIC",
    )

    content_type = extract_field(
        analysis,
        "CONTENT_TYPE",
    )

    trend_relevance = extract_field(
        web_research,
        "TREND_RELEVANCE",
    )

    current_angle = extract_field(
        web_research,
        "CURRENT_ANGLE",
    )

    fact_check = extract_field(
        web_research,
        "IMPORTANT_FACT_CHECK",
    )

    research_summary = extract_field(
        web_research,
        "RESEARCH_SUMMARY",
    )

    generated_at = datetime.now(
        timezone.utc
    ).isoformat()

    return f"""VIDEO_FILE:
{video_name}

GENERATED_AT_UTC:
{generated_at}

TITLE_1:
{title1}

TITLE_2:
{title2}

TITLE_3:
{title3}

RECOMMENDED_TITLE:
{recommended}

DESCRIPTION:
{description}

HASHTAGS:
{hashtags}

CONTENT_SCORE:
{content_score}/10

TREND_SCORE:
{trend_score}/10

TREND_RELEVANCE:
{trend_relevance}/10

RELEVANCE_DECISION:
{relevance_decision}

RELEVANCE_REASON:
{relevance_reason}

TREND_ANGLE:
{trend_angle}

TOPIC:
{video_topic}

CONTENT_TYPE:
{content_type}

KEYWORDS:
{keywords}

CURRENT_WEB_ANGLE:
{current_angle}

IMPORTANT_FACT_CHECK:
{fact_check}

RESEARCH_SUMMARY:
{research_summary}

---
Generated automatically by Code 1.
"""


# ============================================================
# PROCESS ONE VIDEO
# ============================================================

def process_video(
    drive_service,
    gemini_client,
    video,
):
    video_id = video["id"]
    video_name = video["name"]

    logger.info(
        "Processing: %s",
        video_name,
    )

    # --------------------------------------------------------
    # Duplicate protection
    # --------------------------------------------------------

    if metadata_exists(
        drive_service,
        video_name,
    ):
        logger.info(
            "Metadata already exists. Skipping: %s",
            video_name,
        )

        return "SKIPPED"

    # --------------------------------------------------------
    # Size check
    # --------------------------------------------------------

    size_bytes = int(
        video.get("size") or 0
    )

    size_mb = size_bytes / (
        1024 * 1024
    )

    if size_mb > MAX_VIDEO_SIZE_MB:

        raise ValueError(
            f"Video is {size_mb:.1f} MB, "
            f"above configured limit "
            f"of {MAX_VIDEO_SIZE_MB} MB."
        )

    # --------------------------------------------------------
    # Local filename
    # --------------------------------------------------------

    safe_name = (
        video_name
        .replace("/", "_")
        .replace("\\", "_")
    )

    video_path = WORK_DIR / safe_name

    metadata_path = (
        WORK_DIR
        / f"{Path(video_name).stem}.txt"
    )

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    download_drive_file(
        drive_service,
        video_id,
        video_path,
    )

    # --------------------------------------------------------
    # Gemini video upload
    # --------------------------------------------------------

    uploaded_file = upload_video_to_gemini(
        gemini_client,
        video_path,
    )

    try:

        # ----------------------------------------------------
        # Understand video
        # ----------------------------------------------------

        analysis = analyze_video(
            gemini_client,
            uploaded_file,
        )

        logger.info(
            "Video analysis completed."
        )

        # ----------------------------------------------------
        # YouTube research
        # ----------------------------------------------------

        youtube_results = collect_youtube_research(
            analysis
        )

        # ----------------------------------------------------
        # Current web research
        # ----------------------------------------------------

        web_research = web_trend_research(
            gemini_client,
            analysis,
        )

        # ----------------------------------------------------
        # Final metadata
        # ----------------------------------------------------

        metadata = generate_metadata(
            gemini_client,
            analysis,
            youtube_results,
            web_research,
        )

        # ----------------------------------------------------
        # Quality gate
        # ----------------------------------------------------

        validate_metadata(
            metadata
        )

        # ----------------------------------------------------
        # Build TXT
        # ----------------------------------------------------

        final_text = build_metadata_file(
            video_name,
            analysis,
            web_research,
            metadata,
        )

        metadata_path.write_text(
            final_text,
            encoding="utf-8",
        )

        # ----------------------------------------------------
        # Upload TXT to Drive
        # ----------------------------------------------------

        upload_text_file(
            drive_service,
            metadata_path,
            DRIVE_METADATA_FOLDER_ID,
        )

        logger.info(
            "Metadata successfully uploaded: %s",
            metadata_path.name,
        )

        return "SUCCESS"

    finally:

        # Remove local Gemini/temporary files.
        try:
            if video_path.exists():
                video_path.unlink()
        except Exception:
            pass

        try:
            if metadata_path.exists():
                metadata_path.unlink()
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "========== CODE 1 START =========="
    )

    drive_service = get_drive_service()

    gemini_client = get_gemini_client()

    videos = list_input_videos(
        drive_service
    )

    if not videos:

        logger.info(
            "No videos found in 01_INPUT."
        )

        return

    logger.info(
        "Found %d video(s). Maximum this run: %d",
        len(videos),
        MAX_VIDEOS_PER_RUN,
    )

    videos = videos[
        :MAX_VIDEOS_PER_RUN
    ]

    success_count = 0
    skipped_count = 0
    failed_count = 0

    log_lines = []

    for video in videos:

        video_name = video["name"]

        try:

            result = process_video(
                drive_service,
                gemini_client,
                video,
            )

            if result == "SUCCESS":
                success_count += 1

            elif result == "SKIPPED":
                skipped_count += 1

            log_lines.append(
                f"SUCCESS/SKIPPED: {video_name} -> {result}"
            )

        except Exception as exc:

            failed_count += 1

            error_message = (
                f"{type(exc).__name__}: {exc}"
            )

            logger.exception(
                "Failed: %s",
                video_name,
            )

            log_lines.append(
                f"FAILED: {video_name} -> {error_message}"
            )

            # ------------------------------------------------
            # Move failed video to 04_FAILED.
            # ------------------------------------------------

            try:

                move_file_to_failed(
                    drive_service,
                    video["id"],
                )

                log_lines.append(
                    f"MOVED_TO_FAILED: {video_name}"
                )

            except Exception as move_exc:

                logger.exception(
                    "Could not move failed video."
                )

                log_lines.append(
                    "FAILED_TO_MOVE: "
                    f"{move_exc}"
                )

    # --------------------------------------------------------
    # Upload log
    # --------------------------------------------------------

    summary = "\n".join(
        [
            "",
            "========== CODE 1 RUN SUMMARY ==========",
            f"Successful: {success_count}",
            f"Skipped: {skipped_count}",
            f"Failed: {failed_count}",
            "",
            *log_lines,
        ]
    )

    upload_log(
        drive_service,
        summary,
    )

    logger.info(summary)

    logger.info(
        "========== CODE 1 END =========="
    )


if __name__ == "__main__":
    main()
