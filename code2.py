import os
import io
import re
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import (
    MediaIoBaseDownload,
    MediaFileUpload,
)
from googleapiclient.errors import HttpError


# ============================================================
# CONFIGURATION
# ============================================================

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".avi",
    ".mkv",
}

MAX_VIDEO_SIZE_MB = int(
    os.getenv(
        "MAX_VIDEO_SIZE_MB",
        "500"
    )
)

# ------------------------------------------------------------
# YouTube category
# 24 = Entertainment
# ------------------------------------------------------------

YOUTUBE_CATEGORY_ID = os.getenv(
    "YOUTUBE_CATEGORY_ID",
    "24"
)

# ------------------------------------------------------------
# Default language
# ------------------------------------------------------------

YOUTUBE_LANGUAGE = os.getenv(
    "YOUTUBE_LANGUAGE",
    "en"
)

# ------------------------------------------------------------
# Time zone
# ------------------------------------------------------------

TIMEZONE_NAME = "Asia/Kolkata"

# ------------------------------------------------------------
# How many videos Code 2 uploads in ONE run.
#
# IMPORTANT:
# We want:
#
# 9 AM  -> 1 video
# 6 PM  -> 1 video
#
# Therefore keep this at 1.
# ------------------------------------------------------------

VIDEOS_PER_RUN = 1


# ============================================================
# ENVIRONMENT / SECRETS
# ============================================================

YOUTUBE_REFRESH_TOKEN = os.environ[
    "YOUTUBE_REFRESH_TOKEN"
]

GOOGLE_OAUTH_CLIENT_ID = os.environ[
    "GOOGLE_OAUTH_CLIENT_ID"
]

GOOGLE_OAUTH_CLIENT_SECRET = os.environ[
    "GOOGLE_OAUTH_CLIENT_SECRET"
]

DRIVE_INPUT_FOLDER_ID = os.environ[
    "DRIVE_INPUT_FOLDER_ID"
]

DRIVE_METADATA_FOLDER_ID = os.environ[
    "DRIVE_METADATA_FOLDER_ID"
]

DRIVE_UPLOADED_FOLDER_ID = os.environ[
    "DRIVE_UPLOADED_FOLDER_ID"
]


# ============================================================
# GOOGLE SCOPES
# ============================================================

DRIVE_SCOPE = (
    "https://www.googleapis.com/auth/drive"
)

YOUTUBE_SCOPE = (
    "https://www.googleapis.com/auth/youtube.upload"
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger(
    "youtube-shorts-code2"
)


# ============================================================
# WORK DIRECTORY
# ============================================================

WORK_DIR = Path("work")

WORK_DIR.mkdir(
    exist_ok=True
)


# ============================================================
# DRIVE AUTHENTICATION
# ============================================================

def get_drive_service():
    """
    Create authenticated Google Drive client.
    """

    credentials = Credentials(
        token=None,
        refresh_token=(
            os.environ[
                "GOOGLE_DRIVE_REFRESH_TOKEN"
            ]
        ),
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
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


# ============================================================
# YOUTUBE AUTHENTICATION
# ============================================================

def get_youtube_service():
    """
    Create authenticated YouTube API client.

    Uses the separate YouTube refresh token.
    """

    credentials = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        token_uri=(
            "https://oauth2.googleapis.com/token"
        ),
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        scopes=[YOUTUBE_SCOPE],
    )

    return build(
        "youtube",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )


# ============================================================
# LIST INPUT VIDEOS
# ============================================================

def list_input_videos(
    drive_service
):
    """
    Find all videos in 01_INPUT.

    Pagination is used so more than 100 videos
    can be handled.
    """

    videos = []

    page_token = None

    while True:

        query = (
            f"'{DRIVE_INPUT_FOLDER_ID}' "
            "in parents "
            "and trashed = false"
        )

        response = (
            drive_service.files()
            .list(
                q=query,
                pageSize=100,
                pageToken=page_token,
                orderBy="createdTime",
                fields=(
                    "nextPageToken,"
                    "files("
                    "id,"
                    "name,"
                    "mimeType,"
                    "size,"
                    "createdTime"
                    ")"
                ),
            )
            .execute()
        )

        for file in response.get(
            "files",
            []
        ):

            name = file.get(
                "name",
                ""
            )

            suffix = Path(
                name
            ).suffix.lower()

            if suffix in VIDEO_EXTENSIONS:

                videos.append(
                    file
                )

        page_token = response.get(
            "nextPageToken"
        )

        if not page_token:
            break

    return videos


# ============================================================
# FIND MATCHING METADATA
# ============================================================

def find_metadata_file(
    drive_service,
    video_name,
):
    """
    Find the TXT file matching the video.

    Example:

    Funny Video.mp4
    ->
    Funny Video.txt
    """

    metadata_name = (
        Path(video_name).stem
        + ".txt"
    )

    escaped_name = (
        metadata_name.replace(
            "'",
            "''"
        )
    )

    query = (
        f"'{DRIVE_METADATA_FOLDER_ID}' "
        "in parents "
        "and name = "
        f"'{escaped_name}' "
        "and trashed = false"
    )

    response = (
        drive_service.files()
        .list(
            q=query,
            pageSize=10,
            fields=(
                "files("
                "id,"
                "name,"
                "size"
                ")"
            ),
        )
        .execute()
    )

    files = response.get(
        "files",
        []
    )

    if not files:
        return None

    return files[0]


# ============================================================
# DOWNLOAD DRIVE FILE
# ============================================================

def download_drive_file(
    drive_service,
    file_id,
    destination,
):
    """
    Download a Drive file to the GitHub runner.
    """

    request = (
        drive_service.files()
        .get_media(
            fileId=file_id
        )
    )

    with open(
        destination,
        "wb"
    ) as fh:

        downloader = (
            MediaIoBaseDownload(
                fh,
                request
            )
        )

        done = False

        while not done:

            status, done = (
                downloader.next_chunk()
            )

            if status:

                logger.info(
                    "Download progress: %.1f%%",
                    status.progress()
                    * 100,
                )


# ============================================================
# MOVE VIDEO TO 03_UPLOADED
# ============================================================

def move_to_uploaded(
    drive_service,
    file_id,
):
    """
    Move successfully uploaded video
    from 01_INPUT to 03_UPLOADED.
    """

    (
        drive_service.files()
        .update(
            fileId=file_id,
            addParents=DRIVE_UPLOADED_FOLDER_ID,
            removeParents=DRIVE_INPUT_FOLDER_ID,
            fields="id,parents",
        )
        .execute()
    )


# ============================================================
# READ METADATA TXT
# ============================================================

def read_metadata_file(
    metadata_path
):
    """
    Read metadata TXT created by Code 1.
    """

    return metadata_path.read_text(
        encoding="utf-8"
    )


# ============================================================
# EXTRACT METADATA FIELD
# ============================================================

def extract_field(
    text,
    field_name,
):
    """
    Extract a field from Code 1 metadata.
    """

    marker = (
        field_name
        + ":"
    )

    start = text.find(
        marker
    )

    if start == -1:
        return ""

    start += len(
        marker
    )

    remaining = text[start:]

    lines = remaining.splitlines()

    values = []

    for line in lines:

        stripped = line.strip()

        if not stripped:

            if values:
                break

            continue

        # Stop at the next field.
        if (
            ":" in stripped
            and stripped
            .split(
                ":",
                1
            )[0]
            .strip()
            .replace(
                "_",
                ""
            )
            .isalnum()
            and stripped
            .split(
                ":",
                1
            )[0]
            .isupper()
        ):

            break

        values.append(
            stripped
        )

    return (
        "\n".join(values)
        .strip()
    )


# ============================================================
# CLEAN TITLE
# ============================================================

def clean_title(
    title
):
    """
    Clean title without changing its meaning.
    """

    title = (
        title
        .replace(
            "\n",
            " "
        )
        .strip()
    )

    # YouTube title maximum is 100 characters.
    if len(title) > 100:

        title = title[
            :100
        ].rstrip()

    return title


# ============================================================
# CLEAN DESCRIPTION
# ============================================================

def clean_description(
    description,
    hashtags,
):
    """
    Combine description and hashtags.
    """

    description = (
        description
        .strip()
    )

    hashtags = (
        hashtags
        .strip()
    )

    if hashtags:

        if description:

            return (
                description
                + "\n\n"
                + hashtags
            )

        return hashtags

    return description


# ============================================================
# EXTRACT YOUTUBE TAGS
# ============================================================

def extract_tags(
    hashtags,
    keywords,
):
    """
    Convert hashtags/keywords into YouTube tags.
    """

    tags = []

    # Hashtags
    for item in hashtags.split():

        item = item.strip()

        if item.startswith("#"):

            clean = item[
                1:
            ].strip()

            if clean:
                tags.append(
                    clean
                )

    # Keywords
    for item in keywords.split(","):

        clean = item.strip()

        if clean:
            tags.append(
                clean
            )

    # Remove duplicates
    unique_tags = []

    seen = set()

    for tag in tags:

        normalized = (
            tag.lower()
        )

        if normalized in seen:
            continue

        seen.add(
            normalized
        )

        unique_tags.append(
            tag
        )

    # Keep a reasonable number.
    return unique_tags[:15]


# ============================================================
# DOWNLOAD VIDEO
# ============================================================

def prepare_video(
    drive_service,
    video,
):
    """
    Download input video to work directory.
    """

    video_name = video[
        "name"
    ]

    safe_name = (
        video_name
        .replace(
            "/",
            "_"
        )
        .replace(
            "\\",
            "_"
        )
    )

    destination = (
        WORK_DIR
        / safe_name
    )

    download_drive_file(
        drive_service,
        video["id"],
        destination,
    )

    return destination


# ============================================================
# DOWNLOAD METADATA
# ============================================================

def prepare_metadata(
    drive_service,
    metadata_file,
    video_name,
):
    """
    Download matching TXT metadata.
    """

    metadata_name = (
        Path(video_name).stem
        + ".txt"
    )

    destination = (
        WORK_DIR
        / metadata_name
    )

    download_drive_file(
        drive_service,
        metadata_file["id"],
        destination,
    )

    return destination


# ============================================================
# GET NEXT PUBLISH TIME
# ============================================================

def get_publish_time():
    """
    Determine which publishing slot this run belongs to.

    The GitHub workflow will run shortly before:
        09:00
        18:00

    This function receives the desired slot through
    environment variables.

    PUBLISH_HOUR:
        9
        or
        18

    PUBLISH_MINUTE:
        0
    """

    publish_hour = int(
        os.getenv(
            "PUBLISH_HOUR",
            "9"
        )
    )

    publish_minute = int(
        os.getenv(
            "PUBLISH_MINUTE",
            "0"
        )
    )

    # India Standard Time is UTC+05:30.
    ist = timezone(
        timedelta(
            hours=5,
            minutes=30
        )
    )

    now = datetime.now(
        ist
    )

    publish_time = now.replace(
        hour=publish_hour,
        minute=publish_minute,
        second=0,
        microsecond=0,
    )

    # If the calculated publishing time has already passed,
    # use the next day.
    if publish_time <= now:

        publish_time = (
            publish_time
            + timedelta(
                days=1
            )
        )

    return publish_time


# ============================================================
# UPLOAD TO YOUTUBE
# ============================================================

def upload_video(
    youtube,
    video_path,
    title,
    description,
    tags,
    publish_time,
):
    """
    Upload video to YouTube and schedule it.
    """

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": YOUTUBE_CATEGORY_ID,
            "defaultLanguage": YOUTUBE_LANGUAGE,
        },

        "status": {
            "privacyStatus": "private",
            "publishAt": (
                publish_time
                .isoformat()
                .replace(
                    "+00:00",
                    "Z"
                )
            ),
            "selfDeclaredMadeForKids": False,
        },
    }

    logger.info(
        "Uploading to YouTube..."
    )

    logger.info(
        "Title: %s",
        title,
    )

    logger.info(
        "Scheduled publish time: %s",
        publish_time.isoformat(),
    )

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/*",
        chunksize=(
            8 * 1024 * 1024
        ),
        resumable=True,
    )

    request = (
        youtube.videos()
        .insert(
            part=(
                "snippet,status"
            ),
            body=body,
            media_body=media,
        )
    )

    response = None

    while response is None:

        try:

            status, response = (
                request.next_chunk()
            )

            if status:

                logger.info(
                    "YouTube upload progress: %.1f%%",
                    status.progress()
                    * 100,
                )

        except HttpError as exc:

            logger.error(
                "YouTube API error: %s",
                exc,
            )

            raise

    video_id = response.get(
        "id"
    )

    if not video_id:

        raise RuntimeError(
            "YouTube upload returned "
            "no video ID."
        )

    logger.info(
        "YouTube upload successful."
    )

    logger.info(
        "YouTube video ID: %s",
        video_id,
    )

    return video_id


# ============================================================
# PROCESS ONE VIDEO
# ============================================================

def process_video(
    drive_service,
    youtube,
    video,
):
    """
    Process exactly one video.
    """

    video_name = video[
        "name"
    ]

    logger.info(
        "================================"
    )

    logger.info(
        "Processing: %s",
        video_name,
    )

    # --------------------------------------------------------
    # FIND MATCHING METADATA
    # --------------------------------------------------------

    metadata_file = (
        find_metadata_file(
            drive_service,
            video_name,
        )
    )

    if not metadata_file:

        logger.warning(
            "No matching metadata found."
        )

        logger.warning(
            "Leaving video in 01_INPUT."
        )

        return "NO_METADATA"

    # --------------------------------------------------------
    # DOWNLOAD VIDEO
    # --------------------------------------------------------

    video_path = prepare_video(
        drive_service,
        video,
    )

    metadata_path = None

    try:

        # ----------------------------------------------------
        # SIZE CHECK
        # ----------------------------------------------------

        size_mb = (
            video_path.stat().st_size
            / (
                1024
                * 1024
            )
        )

        if (
            size_mb
            > MAX_VIDEO_SIZE_MB
        ):

            raise ValueError(
                f"Video is {size_mb:.1f} MB, "
                f"above the {MAX_VIDEO_SIZE_MB} MB limit."
            )

        # ----------------------------------------------------
        # DOWNLOAD METADATA
        # ----------------------------------------------------

        metadata_path = (
            prepare_metadata(
                drive_service,
                metadata_file,
                video_name,
            )
        )

        metadata_text = (
            read_metadata_file(
                metadata_path
            )
        )

        # ----------------------------------------------------
        # EXTRACT METADATA
        # ----------------------------------------------------

        title = extract_field(
            metadata_text,
            "RECOMMENDED_TITLE",
        )

        description = extract_field(
            metadata_text,
            "DESCRIPTION",
        )

        hashtags = extract_field(
            metadata_text,
            "HASHTAGS",
        )

        keywords = extract_field(
            metadata_text,
            "KEYWORDS",
        )

        # ----------------------------------------------------
        # VALIDATE
        # ----------------------------------------------------

        if not title:

            raise ValueError(
                "RECOMMENDED_TITLE is empty."
            )

        if not description:

            raise ValueError(
                "DESCRIPTION is empty."
            )

        title = clean_title(
            title
        )

        description = (
            clean_description(
                description,
                hashtags,
            )
        )

        tags = extract_tags(
            hashtags,
            keywords,
        )

        # ----------------------------------------------------
        # DETERMINE PUBLISH TIME
        # ----------------------------------------------------

        publish_time = (
            get_publish_time()
        )

        # ----------------------------------------------------
        # UPLOAD
        # ----------------------------------------------------

        youtube_video_id = (
            upload_video(
                youtube,
                video_path,
                title,
                description,
                tags,
                publish_time,
            )
        )

        # ----------------------------------------------------
        # MOVE TO 03_UPLOADED
        # ----------------------------------------------------

        move_to_uploaded(
            drive_service,
            video["id"],
        )

        logger.info(
            "Video moved to 03_UPLOADED."
        )

        logger.info(
            "YouTube ID: %s",
            youtube_video_id,
        )

        return "SUCCESS"

    finally:

        # ----------------------------------------------------
        # CLEAN LOCAL VIDEO
        # ----------------------------------------------------

        try:

            if video_path.exists():
                video_path.unlink()

        except Exception:
            pass

        # ----------------------------------------------------
        # CLEAN LOCAL METADATA
        # ----------------------------------------------------

        if metadata_path:

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
        "========== CODE 2 START =========="
    )

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    drive_service = (
        get_drive_service()
    )

    youtube = (
        get_youtube_service()
    )

    # --------------------------------------------------------
    # FIND VIDEOS
    # --------------------------------------------------------

    videos = (
        list_input_videos(
            drive_service
        )
    )

    if not videos:

        logger.info(
            "No videos found in 01_INPUT."
        )

        logger.info(
            "Nothing to upload."
        )

        return

    logger.info(
        "Found %d video(s) in 01_INPUT.",
        len(videos),
    )

    # --------------------------------------------------------
    # FIND FIRST VIDEO WITH METADATA
    # --------------------------------------------------------
    #
    # We deliberately process only ONE video per run.
    #
    # If the first video has no metadata, we continue
    # searching for the next one that does.
    # --------------------------------------------------------

    processed = 0

    skipped_no_metadata = 0

    for video in videos:

        if (
            processed
            >= VIDEOS_PER_RUN
        ):
            break

        video_name = video[
            "name"
        ]

        metadata_file = (
            find_metadata_file(
                drive_service,
                video_name,
            )
        )

        if not metadata_file:

            logger.info(
                "No metadata for %s. "
                "Checking next video.",
                video_name,
            )

            skipped_no_metadata += 1

            continue

        try:

            result = process_video(
                drive_service,
                youtube,
                video,
            )

            if result == "SUCCESS":

                processed += 1

                logger.info(
                    "Successfully processed: %s",
                    video_name,
                )

            elif result == "NO_METADATA":

                skipped_no_metadata += 1

        except Exception as exc:

            logger.exception(
                "FAILED: %s",
                video_name,
            )

            # IMPORTANT:
            # Do NOT move the video to 03_UPLOADED
            # if YouTube upload failed.
            #
            # It stays in 01_INPUT so the next run
            # can retry it.

            logger.error(
                "Video remains in 01_INPUT "
                "for retry."
            )

            # Stop after a real failure.
            # This avoids accidentally uploading
            # multiple videos in one scheduled run.

            break

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    logger.info(
        "================================"
    )

    logger.info(
        "CODE 2 SUMMARY"
    )

    logger.info(
        "Uploaded this run: %d",
        processed,
    )

    logger.info(
        "Videos without metadata: %d",
        skipped_no_metadata,
    )

    logger.info(
        "========== CODE 2 END =========="
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
