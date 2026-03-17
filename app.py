import streamlit as st
import pandas as pd
import requests
import os
import io
import re
import tempfile
import shutil
from pathlib import Path
from dotenv import load_dotenv
from PIL import Image

# Load environment variables
load_dotenv()

# Try importing required libraries
import json
import uuid
import datetime
import glob
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

try:
    from google import genai
    from google.genai import types
except ImportError:
    st.error("Google GenAI SDK not found. Please install via: pip install google-genai")
    st.stop()


# ==========================================
# HISTORY & GOOGLE DRIVE LOGIC
# ==========================================

HISTORY_FILE = "history.json"
DRIVE_FOLDER_ID = "16P-KCZ2Vdl76csQPvFESZDgQ7QM3to5H"
MAX_HISTORY_ITEMS = 5
SCOPES = ['https://www.googleapis.com/auth/drive']

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_history(history_list):
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history_list, f, indent=4)
    except Exception:
        pass # Ignore in cloud environments without write access

def get_drive_service():
    creds = None
    
    # Check Streamlit Secrets first
    if "google_drive_token" in st.secrets:
        try:
            token_data = dict(st.secrets["google_drive_token"])
            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        except Exception:
            pass
            
    # Fallback to local token.json
    if not creds and os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                if os.path.exists('token.json'):
                    os.remove('token.json')
                return None
        else:
            client_secrets = glob.glob('client_secret*.json')
            if not client_secrets:
                return None
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets[0], SCOPES)
            creds = flow.run_local_server(port=0)
            
        try:
            with open('token.json', 'w') as token:
                token.write(creds.to_json())
        except Exception:
            pass # Ignore read-only filesystems

    try:
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception:
        return None

def upload_batch_to_drive(batch_id, images_list, results_df, custom_folder_name=None):
    """Uploads a batch to Google Drive synchronously. Safe to call from a thread."""
    if not os.path.exists('token.json') and "google_drive_token" not in st.secrets:
        return "Failed: Drive not authenticated in Sidebar"
        
    service = get_drive_service()
    if not service:
        return "Failed: OAuth authentication error"

    try:
        # Create a folder for this batch
        folder_metadata = {
            'name': custom_folder_name.strip() if custom_folder_name and custom_folder_name.strip() else f'Batch_{batch_id}',
            'parents': [DRIVE_FOLDER_ID],
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=folder_metadata, fields='id').execute()
        folder_id = folder.get('id')

        # Upload images
        for img_info in images_list:
            file_metadata = {
                'name': f"{img_info['product_id']}_generated.png",
                'parents': [folder_id]
            }
            media = MediaIoBaseUpload(io.BytesIO(img_info['data']), mimetype='image/png', resumable=True)
            service.files().create(body=file_metadata, media_body=media, fields='id').execute()

        # Upload CSV
        csv_bytes = results_df.to_csv(index=False).encode('utf-8')
        csv_metadata = {
            'name': f"results_{batch_id}.csv",
            'parents': [folder_id]
        }
        csv_media = MediaIoBaseUpload(io.BytesIO(csv_bytes), mimetype='text/csv', resumable=True)
        service.files().create(body=csv_metadata, media_body=csv_media, fields='id').execute()
        
        # Enforce exactly 5 items limit in Drive
        cleanup_old_drive_folders(service)
        
        return "Success"
    except Exception as e:
        return f"Error: {str(e)}"

def cleanup_old_drive_folders(service):
    """Keeps only the 5 most recent folders created in the target Drive folder."""
    try:
        query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, orderBy="createdTime desc", fields="files(id, name, createdTime)").execute()
        folders = results.get('files', [])
        
        # If more than 5, delete the oldest ones
        if len(folders) > MAX_HISTORY_ITEMS:
            for old_folder in folders[MAX_HISTORY_ITEMS:]:
                service.files().delete(fileId=old_folder['id']).execute()
    except Exception:
        pass

# Provide an executor for background tasks
if 'executor' not in st.session_state:
    import concurrent.futures
    st.session_state.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

def schedule_drive_upload_and_log(batch_id, images_list, results_df, total_cost, custom_folder_name=None):
    """Fires off the drive upload in the background and logs to history.json immediately."""
    date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    total_bytes = sum(len(img['data']) for img in images_list)
    size_mb = round(total_bytes / (1024 * 1024), 2)
    num_images = len(images_list)
    
    history_entry = {
        "batch_id": batch_id,
        "date": date_str,
        "images": num_images,
        "cost": round(total_cost, 2),
        "size_mb": size_mb,
        "status": "Uploading..."
    }
    
    hist = load_history()
    hist.insert(0, history_entry)
    if len(hist) > MAX_HISTORY_ITEMS:
        hist = hist[:MAX_HISTORY_ITEMS]
    save_history(hist)
    
    def background_task(b_id, imgs, df, folder_name):
        status = upload_batch_to_drive(b_id, imgs, df, folder_name)
        # Once done, update the status in history.json
        current_hist = load_history()
        for idx, item in enumerate(current_hist):
            if item["batch_id"] == b_id:
                current_hist[idx]["status"] = "Saved to Drive" if status == "Success" else status
                break
        save_history(current_hist)

    st.session_state.executor.submit(background_task, batch_id, images_list, results_df.copy(), custom_folder_name)

# Set up page configurations
st.set_page_config(page_title="AI Product Image Generator", layout="wide", page_icon="🛍️")

st.title("🛍️ AI Product Image Generation Hub")
st.write("Upload a CSV/Excel with product details to batch-generate images via **Nano Banana Pro** (Google Gemini).")

# ==========================================
# MODEL PRICING CONFIGURATION
# ==========================================

USD_TO_INR = 83

MODEL_PRICING = {
    "gemini-3-pro-image-preview": {
        "input_per_1M_tokens": 2.0,
        "image_output": 0.134
    },
    "gemini-3.1-flash-image-preview": {
        "input_per_1M_tokens": 2.0,
        "image_output": 0.134
    },
    "gemini-2.5-flash-image": {
        "input_per_1M_tokens": 2.0,
        "image_output": 0.134
    },
    "gemini-1.5-flash": {
        "input_per_1M_tokens": 0.075,
        "image_output": 0.0003
    },
    "gemini-1.5-pro": {
        "input_per_1M_tokens": 1.25,
        "image_output": 0.007
    },
    "gemini-2.0-flash-exp": {
        "input_per_1M_tokens": 0.10,
        "image_output": 0.0005
    },
    "gemini-2.5-flash": {
        "input_per_1M_tokens": 0.15,
        "image_output": 0.0005
    }
}

# ==========================================
# HELPER FUNCTIONS
# ==========================================

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tiff', '.tif'}

def is_drive_folder_url(url):
    """Check if a URL points to a Google Drive folder."""
    if pd.isna(url):
        return False
    return '/folders/' in str(url)

def is_drive_file_url(url):
    """Check if a URL points to a Google Drive file."""
    if pd.isna(url):
        return False
    url = str(url)
    return '/d/' in url or 'id=' in url

def extract_gdrive_file_id(url):
    """Extracts file ID from a Google Drive file URL."""
    url = str(url).strip()
    match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
    if match:
        return match.group(1)
    return None

def extract_folder_id(url):
    """Extracts folder ID from a Google Drive folder URL."""
    match = re.search(r'/folders/([a-zA-Z0-9_-]+)', str(url))
    return match.group(1) if match else None

def download_drive_folder(folder_url, api_key, label="images"):
    """Downloads all images from a Google Drive folder via Drive API v3.
    Returns list of dicts: [{"name": filename, "image": PIL.Image}, ...]
    """
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return [], "Could not extract folder ID from URL"
    
    images = []
    
    # Step 1: List files in the folder using Drive API v3
    IMAGE_MIMES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/bmp', 'image/tiff'}
    
    list_url = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q": f"'{folder_id}' in parents and trashed = false",
        "key": api_key,
        "fields": "files(id,name,mimeType,size)",
        "pageSize": 100,
    }
    
    try:
        resp = requests.get(list_url, params=params, timeout=15)
        
        if resp.status_code == 403:
            return [], (
                "Drive API access denied. Please enable the Google Drive API for your API key:\n"
                "1. Go to https://console.cloud.google.com/apis/library/drive.googleapis.com\n"
                "2. Select the project associated with your API key\n"
                "3. Click 'Enable'\n"
                "4. Try again"
            )
        elif resp.status_code == 404:
            return [], "Folder not found. Make sure the folder is shared as 'Anyone with the link'."
        
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return [], f"Failed to list folder: {e}"
    
    files = data.get("files", [])
    if not files:
        return [], "Folder is empty or not accessible"
    
    # Step 2: Filter for image files
    image_files = [f for f in files if f.get("mimeType", "") in IMAGE_MIMES]
    if not image_files:
        all_types = [f.get("mimeType", "unknown") for f in files]
        return [], f"No image files found. Files in folder have types: {', '.join(set(all_types))}"
    
    # Step 3: Download each image
    errors = []
    for file_info in sorted(image_files, key=lambda x: x.get("name", "")):
        file_id = file_info["id"]
        file_name = file_info.get("name", "unknown")
        
        dl_url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
        dl_params = {"alt": "media", "key": api_key}
        
        try:
            dl_resp = requests.get(dl_url, params=dl_params, timeout=30)
            dl_resp.raise_for_status()
            
            content_type = dl_resp.headers.get('Content-Type', '')
            if 'text/html' in content_type:
                errors.append(f"{file_name}: Got HTML instead of image (permissions?)")
                continue
            
            img = Image.open(io.BytesIO(dl_resp.content))
            img.load()
            
            base_name = Path(file_name).stem
            images.append({
                "name": base_name,
                "filename": file_name,
                "image": img,
            })
        except requests.exceptions.HTTPError as e:
            errors.append(f"{file_name}: HTTP {e.response.status_code}")
        except Exception as e:
            errors.append(f"{file_name}: {type(e).__name__}: {e}")
    
    if not images:
        detail = "; ".join(errors) if errors else "Unknown reason"
        return images, f"Could not load any images. Errors: {detail}"
    
    return images, None

def download_single_image(url):
    """Downloads a single image from a direct URL or Google Drive file link.
    Returns (PIL.Image, error_str)."""
    url = str(url).strip()
    
    file_id = extract_gdrive_file_id(url)
    if file_id:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
    
    try:
        session = requests.Session()
        response = session.get(url, stream=True, timeout=30)
        response.raise_for_status()
        
        content_type = response.headers.get('Content-Type', '')
        
        # Handle Drive's confirmation page
        if 'text/html' in content_type and file_id:
            confirm_match = re.search(r'confirm=([a-zA-Z0-9_-]+)', response.text)
            if confirm_match:
                confirm_url = f"https://drive.google.com/uc?export=download&confirm={confirm_match.group(1)}&id={file_id}"
                response = session.get(confirm_url, stream=True, timeout=30)
                response.raise_for_status()
                content_type = response.headers.get('Content-Type', '')
        
        if 'text/html' in content_type:
            return None, "Got HTML instead of image. Check sharing settings."
        
        img = Image.open(io.BytesIO(response.content))
        img.load()
        return img, None
        
    except Exception as e:
        return None, str(e)

def download_images_from_url(url, api_key, label="images"):
    """Smart download: handles both folder and file URLs.
    Returns (list of image dicts, error_str)."""
    if not url or pd.isna(url):
        return [], "No URL provided"
    
    url = str(url).strip()
    
    if is_drive_folder_url(url):
        return download_drive_folder(url, api_key, label)
    elif is_drive_file_url(url) or url.startswith('http'):
        img, err = download_single_image(url)
        if img:
            return [{"name": label, "filename": f"{label}.png", "image": img}], None
        return [], err
    else:
        return [], f"Unrecognized URL format: {url[:60]}..."

def save_generated_image(img_data, product_id, row_index=0, output_dir="generated_images"):
    """Saves generated image bytes to disk with unique filenames."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{product_id}_generated.png")
    # If file already exists (duplicate product_id), append row index
    if os.path.exists(filepath):
        filepath = os.path.join(output_dir, f"{product_id}_row{row_index}_generated.png")
    with open(filepath, "wb") as f:
        f.write(img_data)
    return filepath

def build_gemini_content(product_images, reference_images, prompt):
    """Builds the content list for Gemini's generate_content call.
    Labels images so the model knows which is which."""
    
    content_parts = []
    
    # Build a comprehensive label header
    label_lines = []
    
    # Add product images with labels
    img_index = 1
    for pimg in product_images:
        label_lines.append(f"Image {img_index}: Product image - {pimg['name']}")
        img_index += 1
    
    # Add reference images with labels
    for rimg in reference_images:
        name = rimg['name'].lower()
        if 'model' in name or 'face' in name:
            ref_type = "Model face reference"
        elif 'studio' in name or 'setting' in name or 'background' in name or 'bg' in name:
            ref_type = "Studio/background setting reference"
        elif 'style' in name:
            ref_type = "Style reference"
        else:
            ref_type = "Reference image"
        label_lines.append(f"Image {img_index}: {ref_type} - {rimg['name']}")
        img_index += 1
    
    # Combine labels + user prompt
    header = "IMAGE LABELS:\n" + "\n".join(label_lines) + "\n\nINSTRUCTIONS:\n" + prompt
    content_parts.append(header)
    
    # Add all images in order: product first, then references
    for pimg in product_images:
        content_parts.append(pimg['image'])
    for rimg in reference_images:
        content_parts.append(rimg['image'])
    
    return content_parts

def calculate_real_cost(model_name, prompt_tokens, images=1):
    pricing = MODEL_PRICING.get(model_name)

    if not pricing:
        return 0, 0

    input_cost_usd = (prompt_tokens / 1_000_000) * pricing["input_per_1M_tokens"]
    image_cost_usd = images * pricing["image_output"]

    total_usd = input_cost_usd + image_cost_usd
    total_inr = total_usd * USD_TO_INR

    return round(total_usd, 6), round(total_inr, 3)

# ==========================================
# SIDEBAR / CONFIGURATION
# ==========================================

st.sidebar.header("⚙️ Configuration")

# Google Drive Auth
st.sidebar.markdown("### ☁️ Google Drive Auth")
if "google_drive_token" in st.secrets or os.path.exists('token.json'):
    st.sidebar.success("✅ Drive Authenticated")
else:
    client_secrets = glob.glob('client_secret*.json')
    if not client_secrets:
        st.sidebar.error("⚠️ Drive Not Authenticated")
        st.sidebar.caption("Cloud Deployment: Please add your local `token.json` contents to Streamlit Secrets under `[google_drive_token]`.")
    else:
        st.sidebar.warning("🔴 Drive Not Authenticated")
        if st.sidebar.button("🔗 Log in to Google Drive"):
            with st.spinner("Opening browser to authenticate... Check popup tab."):
                service = get_drive_service()
                if service:
                    st.rerun()

st.sidebar.markdown("---")

# Gemini API Key
default_gemini_key = os.getenv("GEMINI_API_KEY", "")
if default_gemini_key:
    st.sidebar.success("✅ API Key loaded from .env")
else:
    st.sidebar.warning("Enter your Gemini API key below.")

gemini_api_key = st.sidebar.text_input(
    "Gemini API Key", 
    value=default_gemini_key, 
    type="password",
    help="Get your free API key from https://aistudio.google.com/apikey"
)
st.sidebar.write("")
st.sidebar.markdown("[Get API Key](https://aistudio.google.com/app/apikey)")

st.sidebar.markdown("---")
st.sidebar.markdown('<div class="stMarkdown"><h3 style="margin-bottom: -15px;">☁️ HISTORY LOG</h3></div>', unsafe_allow_html=True)
st.sidebar.caption("Auto-saves the last 5 batches to Google Drive.")

hist_data = load_history()
if not hist_data:
    st.sidebar.info("No past batches found.")
else:
    for idx, entry in enumerate(hist_data):
        with st.sidebar.expander(f"Batch {entry['batch_id']} - {entry['date'][:10]}"):
            st.caption(f"**Status:** {entry['status']}")
            st.caption(f"**Time:** {entry['date']}")
            st.caption(f"**Images:** {entry['images']} | **Size:** {entry['size_mb']} MB")
            st.caption(f"**Cost:** ₹{entry['cost']}")
            dl_link = f"https://drive.google.com/drive/folders/{DRIVE_FOLDER_ID}"
            st.markdown(f"[📂 Open in Drive]({dl_link})")

st.sidebar.markdown("---")

# --- MODEL SETTINGS ---
st.sidebar.markdown('<div class="stMarkdown"><h3 style="margin-bottom: -15px;">SETTINGS</h3></div>', unsafe_allow_html=True)

# Model selection
# Approximate cost in INR per image generation (assuming ~500 input tokens + 1 image output)
# Note: Google's exact pricing varies by tier/region. These are safe estimates.
PRICING_INR = {
    "gemini-3-pro-image-preview": 3.0,     # Placeholder estimate (Pro is usually more expensive)
    "gemini-3.1-flash-image-preview": 2.5, # ~ $0.03
    "gemini-2.5-flash-image": 2.5,         # ~ $0.03
    "gemini-1.5-flash": 0.05,              # Very cheap, fast
    "gemini-1.5-pro": 1.50,                # High quality, more expensive
    "gemini-2.0-flash-exp": 0.08,          # Experimental 2.0 Flash
    "gemini-2.5-flash": 0.10,              # Newest Flash
}

model_name = st.sidebar.selectbox(
    "Model",
    options=[
        "gemini-3-pro-image-preview",
        "gemini-3.1-flash-image-preview",
        "gemini-2.5-flash-image",
        "gemini-2.5-flash",
        "gemini-2.0-flash-exp",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ],
    format_func=lambda x: f"{ {
        'gemini-3-pro-image-preview': '🌟 Nano Banana Pro (Gemini 3 Pro)',
        'gemini-3.1-flash-image-preview': '⚡ Nano Banana 2 (Gemini 3.1 Flash)',
        'gemini-2.5-flash-image': '💨 Gemini 2.5 Flash Image',
        'gemini-2.5-flash': '⚡ Gemini 2.5 Flash (Newest)',
        'gemini-2.0-flash-exp': '🧪 Gemini 2.0 Flash (Experimental)',
        'gemini-1.5-pro': '🌟 Gemini 1.5 Pro (Best Quality)',
        'gemini-1.5-flash': '💨 Gemini 1.5 Flash (Cheapest/Fastest)',
    }.get(x, x) } — ~₹{PRICING_INR.get(x, 0.0)}/img",
    index=0,
    help="Nano Banana Pro = best quality. Nano Banana 2 = faster. 1.5/2.0 Flash models are cheapest."
)

# System prompt
default_system_prompt = """You are an elite AI image-generation engine explicitly designed for high-end fashion e-commerce.

[CRITICAL MASTER RULES - OBEY EXACTLY]
1. PRODUCT IDENTITY IS ABSOLUTE: You MUST produce an image where the garments/products are 100% IDENTICAL to the provided Product Image reference. Do NOT alter the color, fabric texture, embroidery, design, buttons, or fit under any circumstance.
2. MODEL FACE INCORPORATION: If a "Model face reference" is provided, you MUST map the facial features (eyes, nose, mouth structure, jawline) exactly to your generated model. 
3. PREVENT FACIAL DEFORMITIES: Enforce strict anatomical symmetry. Ensure eyes are exactly proportional and perfectly aligned. Do NOT generate crossed eyes, asymmetrical pupils, melting skin, or any uncanny "plastic" AI artifacts. Keep expressions calm, confident, and professional.
4. STUDIO & SETTING: If a "Studio/background setting reference" is provided, replicate the environment, lighting, architecture, and shadows faithfully. If no background is instructed, default to a clean, professional fashion studio backdrop.
5. NO HALLUCINATIONS: Do not invent details, accessories, or background elements not requested or not present in the reference imagery.

Interpret all user input as literal, non-negotiable visual directives for image composition. If conflicts occur, prioritize: Product Accuracy -> Facial Symmetry & Likeness -> Background Integrity."""

system_prompt = st.sidebar.text_area(
    "System Prompt",
    value=default_system_prompt,
    height=250,
    help="Instructions that guide image generation."
)

if not gemini_api_key:
    st.info("👈 Enter your Gemini API key in the sidebar to get started.")
    st.stop()

# Initialize client
client = genai.Client(api_key=gemini_api_key)

# ==========================================
# MAIN APP BODY
# ==========================================

tab_direct, tab_csv = st.tabs(["📤 Direct Upload", "📑 CSV / Drive Mode"])

with tab_direct:
    st.write("### 📤 Direct Upload Interface")
    
    if "upload_rows" not in st.session_state:
        st.session_state["upload_rows"] = [{"id": 0}]
        st.session_state["row_counter"] = 1
        
    def add_row():
        st.session_state["upload_rows"].append({"id": st.session_state["row_counter"]})
        st.session_state["row_counter"] += 1
        
    def duplicate_row(idx):
        original_id = st.session_state["upload_rows"][idx]["id"]
        # Streamlit file_uploaders cannot be populated programmatically.
        # But we can store the copied files in session state to use later.
        prod_name = st.session_state.get(f"prod_name_{original_id}", "")
        prompt_val = st.session_state.get(f"prompt_{original_id}", "")
        
        prod_imgs = st.session_state.get(f"prod_imgs_{original_id}", [])
        if not prod_imgs:
            prod_imgs = st.session_state.get(f"copied_prod_imgs_{original_id}", [])
            
        ref_imgs = st.session_state.get(f"ref_imgs_{original_id}", [])
        if not ref_imgs:
            ref_imgs = st.session_state.get(f"copied_ref_imgs_{original_id}", [])
        
        new_id = st.session_state["row_counter"]
        st.session_state["upload_rows"].insert(idx + 1, {"id": new_id})
        
        # Pre-fill text inputs in session state before widget renders
        st.session_state[f"prod_name_{new_id}"] = prod_name + " (Copy)"
        st.session_state[f"prompt_{new_id}"] = prompt_val
        st.session_state[f"copied_prod_imgs_{new_id}"] = prod_imgs
        st.session_state[f"copied_ref_imgs_{new_id}"] = ref_imgs
        st.session_state["row_counter"] += 1

    def delete_row(idx):
        if len(st.session_state["upload_rows"]) > 1:
            st.session_state["upload_rows"].pop(idx)
            
    def remove_copied_image(state_key, idx):
        if state_key in st.session_state:
            new_list = list(st.session_state[state_key])
            if 0 <= idx < len(new_list):
                new_list.pop(idx)
                st.session_state[state_key] = new_list

    # Render table headers
    cols = st.columns([0.5, 2, 2.5, 2.5, 2.5, 2])
    cols[0].write("**S No.**")
    cols[1].write("**Product Name**")
    cols[2].write("**Product Image(s)**")
    cols[3].write("**Reference Image(s)**")
    cols[4].write("**Prompt**")
    cols[5].write("**Actions**")
    
    for i, row in enumerate(st.session_state["upload_rows"]):
        r_id = row["id"]
        cols = st.columns([0.5, 2, 2.5, 2.5, 2.5, 2])
        
        with cols[0]:
            st.write(f"**{i+1}**")
            
        with cols[1]:
            st.text_input("Name", key=f"prod_name_{r_id}", label_visibility="collapsed", placeholder="Name...")
            
        with cols[2]:
            st.file_uploader("Products", key=f"prod_imgs_{r_id}", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, label_visibility="collapsed")
            copied_prod = st.session_state.get(f"copied_prod_imgs_{r_id}", [])
            if copied_prod and not st.session_state.get(f"prod_imgs_{r_id}"):
                for img_idx, img in enumerate(copied_prod):
                    c1, c2 = st.columns([0.85, 0.15])
                    with c1:
                        st.caption(f"📎 {img.name}")
                    with c2:
                        st.button("✖", key=f"rm_p_{r_id}_{img_idx}", on_click=remove_copied_image, args=(f"copied_prod_imgs_{r_id}", img_idx), help="Remove image")
            
        with cols[3]:
            st.file_uploader("References", key=f"ref_imgs_{r_id}", type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True, label_visibility="collapsed")
            copied_ref = st.session_state.get(f"copied_ref_imgs_{r_id}", [])
            if copied_ref and not st.session_state.get(f"ref_imgs_{r_id}"):
                for img_idx, img in enumerate(copied_ref):
                    c1, c2 = st.columns([0.85, 0.15])
                    with c1:
                        st.caption(f"📎 {img.name}")
                    with c2:
                        st.button("✖", key=f"rm_r_{r_id}_{img_idx}", on_click=remove_copied_image, args=(f"copied_ref_imgs_{r_id}", img_idx), help="Remove image")
            
        with cols[4]:
            st.text_area("Prompt", key=f"prompt_{r_id}", label_visibility="collapsed", placeholder="Prompt...", height=68)
            
        with cols[5]:
            act_cols = st.columns(3)
            with act_cols[0]:
                st.button("➕", key=f"add_{r_id}", on_click=add_row, help="Add row at bottom")
            with act_cols[1]:
                st.button("📋", key=f"dup_{r_id}", on_click=duplicate_row, args=(i,), help="Duplicate row (copies text)")
            with act_cols[2]:
                st.button("🗑️", key=f"del_{r_id}", on_click=delete_row, args=(i,), help="Delete row")
                
    st.markdown("---")
    
    # System prompt at the bottom of the table
    st.write("### 🤖 Generation Settings")
    system_prompt_direct = st.text_area(
        "System Prompt (Default)",
        value=default_system_prompt,
        height=200,
        help="Instructions that guide image generation. You can change this if needed."
    )
    
    max_workers_direct = st.slider("Parallel Workers (Direct Upload)", 1, 8, 3, help="Number of parallel generation requests")
    
    # Forecast cost for Direct Upload
    estimated_tokens = 600
    est_usd, est_inr = calculate_real_cost(
        model_name,
        estimated_tokens,
        images=1
    )
    # Count rows that have both name and images
    valid_rows_count = sum(
        1 for row in st.session_state["upload_rows"] 
        if st.session_state.get(f"prod_name_{row['id']}", "").strip() 
        and (st.session_state.get(f"prod_imgs_{row['id']}", []) or st.session_state.get(f"copied_prod_imgs_{row['id']}", []))
    )
    forecast_total = est_inr * valid_rows_count
    
    if valid_rows_count > 0:
        st.info(f"**Estimated Cost for {valid_rows_count} items:** ~₹{round(forecast_total, 2)} (₹{est_inr}/image)")
    
    custom_folder_direct = st.text_input("Drive Save Folder Name (Optional)", key="drive_folder_direct", placeholder="e.g. Summer Collection 2026", help="Overrides the default Batch ID folder name in Google Drive.")
    
    with st.expander("🛠️ Advanced Gen Settings", expanded=False):
        gen_seed = st.number_input("Seed (0 for random)", min_value=0, max_value=2147483647, value=1000000, step=1, help="Use a specific seed for reproducible results (Max: 2147483647).")
        gen_aspect_ratio = st.selectbox("Aspect Ratio", ["1:1", "9:16", "16:9", "4:3", "3:4"], index=4)

    if st.button("🚀 Start Generation (Direct Upload)", use_container_width=True):
        # Gather data from session state
        items_to_process = []
        for row in st.session_state["upload_rows"]:
            r_id = row["id"]
            p_name = st.session_state.get(f"prod_name_{r_id}", "").strip()
            p_prompt = st.session_state.get(f"prompt_{r_id}", "").strip()
            p_imgs = st.session_state.get(f"prod_imgs_{r_id}", [])
            if not p_imgs:
                p_imgs = st.session_state.get(f"copied_prod_imgs_{r_id}", [])
                
            r_imgs = st.session_state.get(f"ref_imgs_{r_id}", [])
            if not r_imgs:
                r_imgs = st.session_state.get(f"copied_ref_imgs_{r_id}", [])
            
            if p_name and p_imgs:
                items_to_process.append({
                    "product_id": p_name,
                    "prompt": p_prompt,
                    "prod_files": p_imgs,
                    "ref_files": r_imgs
                })
        
        if not items_to_process:
            st.error("⚠️ Please fill in at least one row with a Product Name and Product Image(s).")
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            import concurrent.futures
            import threading
            
            total = len(items_to_process)
            completed = [0]
            lock = threading.Lock()
            
            all_results = [None] * total
            all_gen_images = []
            
            def process_direct_row(index, item):
                product_id = item["product_id"]
                
                # Convert UploadedFiles to dicts with PIL Images
                product_imgs = []
                for f in item["prod_files"]:
                    try:
                        f.seek(0)
                        img = Image.open(f)
                        img.load()
                        product_imgs.append({"name": Path(f.name).stem, "filename": f.name, "image": img})
                    except Exception as e:
                        return {"product_id": product_id, "status": "Failed", "generated_file": "", "error": f"Failed to open product image {f.name}: {e}"}, None
                        
                reference_imgs = []
                for f in item["ref_files"]:
                    try:
                        f.seek(0)
                        img = Image.open(f)
                        img.load()
                        reference_imgs.append({"name": Path(f.name).stem, "filename": f.name, "image": img})
                    except Exception as e:
                        return {"product_id": product_id, "status": "Failed", "generated_file": "", "error": f"Failed to open reference image {f.name}: {e}"}, None
                
                # Generate
                try:
                    content_parts = build_gemini_content(product_imgs, reference_imgs, item["prompt"])
                    
                    final_sys_prompt = system_prompt_direct + f"\n\nOUTPUT ASPECT RATIO: {gen_aspect_ratio}"
                    
                    config_kwargs = {
                        "response_modalities": ["IMAGE", "TEXT"],
                        "system_instruction": final_sys_prompt,
                    }
                    if int(gen_seed) != 0:
                        config_kwargs["seed"] = int(gen_seed)
                        
                    response = client.models.generate_content(
                        model=model_name,
                        contents=content_parts,
                        config=types.GenerateContentConfig(**config_kwargs),
                    )
                    
                    usage = getattr(response, "usage_metadata", None)
                    prompt_tokens = 0
                    if usage:
                        prompt_tokens = getattr(usage, "prompt_token_count", 0)
                        
                    usd_cost, inr_cost = calculate_real_cost(
                        model_name,
                        prompt_tokens,
                        images=1
                    )
                    
                    if response.candidates:
                        for part in response.candidates[0].content.parts:
                            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                                gen_file = save_generated_image(part.inline_data.data, product_id, row_index=index)
                                return (
                                    {
                                        "product_id": product_id, 
                                        "status": "Success", 
                                        "Prompt Tokens": prompt_tokens,
                                        "Cost (USD)": usd_cost,
                                        "Cost (INR)": inr_cost, 
                                        "generated_file": gen_file, 
                                        "error": ""
                                    },
                                    {"product_id": product_id, "file": gen_file, "data": part.inline_data.data}
                                )
                                
                    text_resp = ""
                    if response.candidates:
                        for part in response.candidates[0].content.parts:
                            if part.text: text_resp = part.text
                    err_msg = f"No image returned. Model said: {text_resp[:200]}" if text_resp else "No image in response"
                    return {"product_id": product_id, "status": "Failed", "Prompt Tokens": 0, "Cost (USD)": 0.0, "Cost (INR)": 0.0, "generated_file": "", "error": err_msg}, None
                except Exception as e:
                    return {"product_id": product_id, "status": "Failed", "Prompt Tokens": 0, "Cost (USD)": 0.0, "Cost (INR)": 0.0, "generated_file": "", "error": str(e)}, None

            status_text.text(f"🚀 Processing {total} products with {max_workers_direct} workers...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers_direct) as executor:
                future_to_index = {executor.submit(process_direct_row, i, item): i for i, item in enumerate(items_to_process)}
                for future in concurrent.futures.as_completed(future_to_index):
                    idx = future_to_index[future]
                    try:
                        res, gen_img = future.result()
                        all_results[idx] = res
                        if gen_img:
                            with lock: all_gen_images.append(gen_img)
                    except Exception as e:
                        all_results[idx] = {"product_id": items_to_process[idx]["product_id"], "status": "Failed", "Prompt Tokens": 0, "Cost (USD)": 0.0, "Cost (INR)": 0.0, "generated_file": "", "error": str(e)}
                    
                    with lock:
                        completed[0] += 1
                        progress_bar.progress(completed[0] / total)
                        success = sum(1 for r in all_results if r and r["status"] == "Success")
                        status_text.text(f"⚡ {completed[0]}/{total} done — {success} successful")
            
            st.session_state["results"] = [r for r in all_results if r]
            st.session_state["generated_images"] = all_gen_images
            
            # Trigger Background Drive Upload
            if all_gen_images:
                b_id = str(uuid.uuid4())[:8]
                r_df = pd.DataFrame(st.session_state["results"])
                t_cost = float(r_df["Cost (INR)"].sum()) if "Cost (INR)" in r_df.columns else 0.0
                schedule_drive_upload_and_log(b_id, all_gen_images, r_df, t_cost, custom_folder_direct)

with tab_csv:
    st.write("### 📑 Bulk Generation via CSV & Drive")
    uploaded_file = st.file_uploader("Upload CSV or Excel file", type=["csv", "xlsx"])

    with st.expander("ℹ️ How to format your file"):
        st.markdown("""
        Your file must contain these columns:
        * `product_id` — Unique product identifier
        * `product_image_url` — Google Drive **folder** or **file** link for product images
        * `reference_image_url` — Google Drive **folder** or **file** link for reference images
        * `prompt` — Generation instructions
        
        **📁 Folder support:**
        - You can link to a **Drive folder** containing multiple images
        - All images in the folder will be downloaded and sent to the model
        
        **🏷️ Reference image naming convention:**
        - `Model Face.jpg` → Used as the model's face reference
        - `Studio Setting.png` → Used as background/environment reference
        - Other names → General style references
        """)

    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith('.csv'):
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)
        except Exception as e:
            st.error(f"Error reading file: {e}")
            st.stop()
            
        st.write("### 📊 Data Preview")
        st.dataframe(df.head(10))
        
        required_columns = ["product_id", "product_image_url", "reference_image_url", "prompt"]
        missing_cols = [col for col in required_columns if col not in df.columns]
        if missing_cols:
            st.error(f"⚠️ Missing columns: **{', '.join(missing_cols)}**")
            
        # Parallel workers control
        max_workers = st.sidebar.slider("Parallel Workers (CSV)", 1, 8, 3, help="Number of parallel generation requests")
        
        # Forecast cost for CSV mode
        estimated_tokens = 600
        est_usd, est_inr = calculate_real_cost(
            model_name,
            estimated_tokens,
            images=1
        )
        forecast_total = est_inr * len(df)
        
        if len(df) > 0:
            st.info(f"**Estimated Cost for {len(df)} items:** ~₹{round(forecast_total, 2)} (₹{est_inr}/image)")
        
        custom_folder_csv = st.text_input("Drive Save Folder Name (Optional)", key="drive_folder_csv", placeholder="e.g. Summer Collection 2026", help="Overrides the default Batch ID folder name in Google Drive.")
        
        if st.button("🚀 Start Parallel Generation (CSV)", use_container_width=True):
            progress_bar = st.progress(0)
            status_text = st.empty()
            results_container = st.container()
            
            import concurrent.futures
            import threading
            
            total = len(df)
            completed = [0]  # Use list for thread-safe mutation
            lock = threading.Lock()
            
            all_results = [None] * total  # Pre-allocate to maintain order
            all_gen_images = []
            
            def process_csv_row(index, row):
                """Process a single product row (download + generate). Returns (result_dict, gen_image_dict_or_none)."""
                product_id = row['product_id']
                prod_url = row['product_image_url']
                ref_url = row['reference_image_url']
                prompt_text = row['prompt']
                
                # Download product images
                product_imgs, prod_err = download_images_from_url(prod_url, gemini_api_key, label="product")
                if not product_imgs:
                    return {"product_id": product_id, "status": "Failed", "Prompt Tokens": 0, "Cost (USD)": 0.0, "Cost (INR)": 0.0, "generated_file": "", "error": f"Product: {prod_err}"}, None
                
                # Download reference images
                reference_imgs, ref_err = download_images_from_url(ref_url, gemini_api_key, label="reference")
                if not reference_imgs:
                    return {"product_id": product_id, "status": "Failed", "Prompt Tokens": 0, "Cost (USD)": 0.0, "Cost (INR)": 0.0, "generated_file": "", "error": f"Reference: {ref_err}"}, None
                
                # Generate with Gemini
                try:
                    content_parts = build_gemini_content(product_imgs, reference_imgs, prompt_text)
                    
                    final_sys_prompt = system_prompt + f"\n\nOUTPUT ASPECT RATIO: {gen_aspect_ratio}"
                    
                    config_kwargs = {
                        "response_modalities": ["IMAGE", "TEXT"],
                        "system_instruction": final_sys_prompt,
                    }
                    if int(gen_seed) != 0:
                        config_kwargs["seed"] = int(gen_seed)
                        
                    response = client.models.generate_content(
                        model=model_name,
                        contents=content_parts,
                        config=types.GenerateContentConfig(**config_kwargs),
                    )
                    
                    usage = getattr(response, "usage_metadata", None)
                    prompt_tokens = 0
                    if usage:
                        prompt_tokens = getattr(usage, "prompt_token_count", 0)
                        
                    usd_cost, inr_cost = calculate_real_cost(
                        model_name,
                        prompt_tokens,
                        images=1
                    )
                    
                    # Extract generated image
                    if response.candidates:
                        for part in response.candidates[0].content.parts:
                            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                                gen_file = save_generated_image(part.inline_data.data, product_id, row_index=index)
                                return (
                                    {
                                        "product_id": product_id, 
                                        "status": "Success", 
                                        "Prompt Tokens": prompt_tokens,
                                        "Cost (USD)": usd_cost,
                                        "Cost (INR)": inr_cost, 
                                        "generated_file": gen_file, 
                                        "error": ""
                                    },
                                    {"product_id": product_id, "file": gen_file, "data": part.inline_data.data}
                                )
                    
                    # No image in response
                    text_resp = ""
                    if response.candidates:
                        for part in response.candidates[0].content.parts:
                            if part.text:
                                text_resp = part.text
                    error_msg = f"No image returned. Model said: {text_resp[:200]}" if text_resp else "No image in response"
                    return {"product_id": product_id, "status": "Failed", "Prompt Tokens": 0, "Cost (USD)": 0.0, "Cost (INR)": 0.0, "generated_file": "", "error": error_msg}, None
                    
                except Exception as e:
                    error_detail = str(e)
                    cause = getattr(e, '__cause__', None)
                    if cause:
                        error_detail += f" | Cause: {cause}"
                    return {"product_id": product_id, "status": "Failed", "Prompt Tokens": 0, "Cost (USD)": 0.0, "Cost (INR)": 0.0, "generated_file": "", "error": error_detail}, None
            
            # Submit all tasks in parallel
            status_text.text(f"🚀 Processing {total} products with {max_workers} parallel workers...")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_index = {}
                for index, row in df.iterrows():
                    future = executor.submit(process_csv_row, index, row)
                    future_to_index[future] = index
                
                # Collect results as they complete
                for future in concurrent.futures.as_completed(future_to_index):
                    idx = future_to_index[future]
                    try:
                        result, gen_img = future.result()
                        all_results[idx] = result
                        if gen_img:
                            with lock:
                                all_gen_images.append(gen_img)
                    except Exception as e:
                        product_id = df.iloc[idx]['product_id']
                        all_results[idx] = {"product_id": product_id, "status": "Failed", "Prompt Tokens": 0, "Cost (USD)": 0.0, "Cost (INR)": 0.0, "generated_file": "", "error": str(e)}
                    
                    with lock:
                        completed[0] += 1
                        progress_bar.progress(completed[0] / total)
                        done = completed[0]
                        success = sum(1 for r in all_results if r and r["status"] == "Success")
                        status_text.text(f"⚡ {done}/{total} done — {success} successful")
            
            status_text.text(f"✅ All {total} products processed! ({sum(1 for r in all_results if r and r['status'] == 'Success')} successful)")
            
            # Store in session state
            st.session_state["results"] = [r for r in all_results if r]
            st.session_state["generated_images"] = all_gen_images
            
            # Trigger Background Drive Upload
            if all_gen_images:
                b_id = str(uuid.uuid4())[:8]
                r_df = pd.DataFrame(st.session_state["results"])
                t_cost = float(r_df["Cost (INR)"].sum()) if "Cost (INR)" in r_df.columns else 0.0
                schedule_drive_upload_and_log(b_id, all_gen_images, r_df, t_cost, custom_folder_csv)


# ==========================================
# DISPLAY RESULTS (persists across reruns)
# ==========================================
st.markdown("---")

if "results" in st.session_state and st.session_state["results"]:
    results = st.session_state["results"]
    generated_images = st.session_state.get("generated_images", [])
    
    st.write("### 📝 Results")
    results_df = pd.DataFrame(results)
    
    total_cost_val = float(results_df["Cost (INR)"].sum()) if "Cost (INR)" in results_df.columns else 0.0
    num_results = len(results_df)
    avg_cost = float(total_cost_val / num_results) if num_results > 0 else 0.0
    
    st.markdown(
        f"""
        <div style="
        position:sticky;
        top:0;
        background:#0e1117;
        padding:12px;
        border-radius:10px;
        z-index:999;
        font-size:18px;
        border:1px solid #333;
        margin-bottom:15px;
        ">

        💸 <b>Total Cost</b> : ₹{round(total_cost_val, 2)}  
        🖼️ Images Generated : {num_results}  
        📊 Avg Cost/Image : ₹{round(avg_cost, 3)}

        </div>
        """,
        unsafe_allow_html=True
    )
    
    st.dataframe(results_df)
    
    # Image gallery
    if generated_images:
        st.write("### 🖼️ Generated Images")
        
        # Download All as ZIP button at the top
        import zipfile
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for img_info in generated_images:
                zf.writestr(f"{img_info['product_id']}_generated.png", img_info["data"])
        zip_buffer.seek(0)
        
        st.download_button(
            label="📦 Download All Images as ZIP",
            data=zip_buffer.getvalue(),
            file_name="generated_images.zip",
            mime="application/zip",
            key="dl_all_zip",
            use_container_width=True,
        )
        
        cols = st.columns(min(3, len(generated_images)))
        for i, img_info in enumerate(generated_images):
            with cols[i % 3]:
                st.image(img_info["data"], caption=f"Product: {img_info['product_id']}", use_container_width=True)
                st.download_button(
                    label=f"📥 Download",
                    data=img_info["data"],
                    file_name=f"{img_info['product_id']}_generated.png",
                    mime="image/png",
                    key=f"dl_img_{i}",
                )
    
    # CSV export
    csv_export = results_df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="📥 Download Results CSV",
        data=csv_export,
        file_name='generated_results.csv',
        mime='text/csv',
        key="dl_results",
    )
