import base64
import os
import random
import time
from io import BytesIO
from typing import List, Dict, Optional, Tuple

import keyring
from PIL import Image
from twocaptcha import TwoCaptcha
from playwright.sync_api import Page, Locator

def resolve_api_key() -> Optional[str]:
    """2Captcha key: env var first, then the OS keychain."""
    key = os.environ.get("TWOCAPTCHA_API_KEY")
    if key:
        return key
    try:
        return keyring.get_password("twocaptcha", "api_key")
    except Exception:
        return None

_solver: Optional[TwoCaptcha] = None

def get_solver() -> Optional[TwoCaptcha]:
    """TwoCaptcha client, constructed lazily and reused."""
    global _solver
    if _solver is None:
        key = resolve_api_key()
        if not key:
            return None
        _solver = TwoCaptcha(
            apiKey=key,
            defaultTimeout=120,    # Max queue wait time in seconds
            pollingInterval=5      # Check every 5 seconds
        )
    return _solver

def _no_key_message(prefix: str) -> None:
    print(f"{prefix} No 2Captcha key found. Set TWOCAPTCHA_API_KEY, or store one with:\n"
          "python3 -c \"import keyring; keyring.set_password('twocaptcha','api_key','...')\"")


def parse_coordinates(code_val) -> List[Dict[str, float]]:
    """Normalizes 2Captcha coordinate responses (string, dict, or list) into a list of {x, y} floats."""
    if not code_val:
        return []
    if isinstance(code_val, list):
        return [{'x': float(p['x']), 'y': float(p['y'])} for p in code_val]
    if isinstance(code_val, dict):
        if 'code' in code_val:
            return parse_coordinates(code_val['code'])
        return [{'x': float(code_val['x']), 'y': float(code_val['y'])}]
    
    pts = []
    text = str(code_val).replace("coordinates:", "").strip()
    for pair in text.split(";"):
        if "," in pair:
            parts = pair.strip().split(",")
            d = {}
            for part in parts:
                if "=" in part:
                    k, v = part.split("=", 1)
                    try:
                        d[k.strip()] = float(v.strip())
                    except ValueError:
                        pass
            if "x" in d and "y" in d:
                pts.append(d)
    return pts


# =====================================================================
# 1. Token-Based Solver (Standard hCaptcha / Enterprise)
# =====================================================================

def solve_token_hcaptcha(page: Page, timeout_ms: int = 3000, rqdata: Optional[str] = None) -> bool:
    """
    Detects hCaptcha, requests a token from 2Captcha, injects it, 
    and fires registered callbacks.
    """
    # 1. Wait briefly to ensure dynamically rendered widgets appear
    try:
        page.wait_for_selector("[data-sitekey], iframe[src*='hcaptcha.com']", timeout=timeout_ms)
    except Exception:
        # Check frames as well
        found = False
        for f in page.frames:
            try:
                if f.locator("[data-sitekey], iframe[src*='hcaptcha.com']").count() > 0:
                    found = True
                    break
            except Exception:
                pass
        if not found:
            return True  # No challenge detected on this form step

    container = page.query_selector("[data-sitekey]")
    iframe = page.query_selector("iframe[src*='hcaptcha.com']")

    if not container and not iframe:
        for f in page.frames:
            container = f.query_selector("[data-sitekey]")
            if container:
                break
            iframe = f.query_selector("iframe[src*='hcaptcha.com']")
            if iframe:
                break

    # 2. Extract sitekey
    sitekey = None
    if container:
        sitekey = container.get_attribute("data-sitekey")
    if not sitekey and iframe:
        src = iframe.get_attribute("src") or ""
        for part in src.split("&"):
            if "sitekey=" in part:
                sitekey = part.split("sitekey=")[1]
                break

    if not sitekey:
        print("[2Captcha] Challenge detected but failed to extract sitekey.")
        return False

    print(f"[2Captcha] Submitting token request for sitekey: {sitekey}")
    
    kwargs = {}
    if rqdata:
        kwargs["data"] = rqdata

    solver = get_solver()
    if solver is None:
        _no_key_message("[2Captcha]")
        return False

    try:
        result = solver.hcaptcha(sitekey=sitekey, url=page.url, **kwargs)
        token = result.get("code") if isinstance(result, dict) else result
        if not token:
            return False
    except Exception as e:
        print(f"[2Captcha Error]: {e}")
        return False

    # 3. Inject token and fire callbacks
    inject_script = """(token) => {
        ['h-captcha-response', 'g-recaptcha-response'].forEach(name => {
            document.querySelectorAll(`[name="${name}"]`).forEach(el => {
                el.value = token;
                el.innerHTML = token;
            });
        });

        // Trigger container callback if registered
        const widget = document.querySelector('[data-sitekey][data-callback]');
        if (widget) {
            const cbName = widget.getAttribute('data-callback');
            if (cbName && typeof window[cbName] === 'function') {
                window[cbName](token);
                return;
            }
        }

        // Direct fallback for runtime instances
        if (window.hcaptcha && typeof window.hcaptcha.setData === 'function') {
            try { window.hcaptcha.setData(token); } catch (_) {}
        }
    }"""
    try:
        page.evaluate(inject_script, token)
        for f in page.frames:
            try:
                f.evaluate(inject_script, token)
            except Exception:
                pass
    except Exception as e:
        print(f"[2Captcha injection notice]: {e}")

    print("[2Captcha] Token injected successfully.")
    return True


# =====================================================================
# 2. Coordinate-Based Solver (Visual Grids / Multi-Select)
# =====================================================================

def solve_coordinate_grid(page: Page, container_locator: Locator, instruction_text: str) -> bool:
    """
    Captures an image of a multi-choice visual grid, normalizes coordinates 
    against DPI/Retina differences, and simulates user clicks.
    """
    solver = get_solver()
    if solver is None:
        _no_key_message("[2Captcha Grid]")
        return False

    try:
        # 1. Screenshot element and measure pixel density scale
        img_bytes = container_locator.screenshot()
        box = container_locator.bounding_box()
        if not box:
            return False

        # Compute scaling factor between actual image pixels and CSS viewport pixels
        with Image.open(BytesIO(img_bytes)) as pil_img:
            img_width, img_height = pil_img.size
        
        scale_x = box["width"] / img_width
        scale_y = box["height"] / img_height

        b64_image = base64.b64encode(img_bytes).decode("utf-8")
        print(f"[2Captcha] Submitting grid task: '{instruction_text}' (DPI Scale: {scale_x:.2f}x, {scale_y:.2f}y)")

        # 2. Submit coordinates task (passing b64_image as first argument)
        result = solver.coordinates(
            b64_image,
            hintText=instruction_text
        )

        raw_points = result.get("code") or result.get("request") or []
        points = parse_coordinates(raw_points)
        if not points:
            print("[2Captcha] Worker returned no coordinate points.")
            return False

        # 3. Translate and click with scaled offsets
        for pt in points:
            raw_x = float(pt["x"]) * scale_x
            raw_y = float(pt["y"]) * scale_y
            
            target_x = box["x"] + raw_x
            target_y = box["y"] + raw_y

            page.mouse.move(target_x, target_y)
            page.wait_for_timeout(random.randint(120, 200))
            page.mouse.click(target_x, target_y)
            page.wait_for_timeout(random.randint(250, 400))

        return True

    except Exception as e:
        print(f"[2Captcha Grid Error]: {e}")
        return False


# =====================================================================
# 3. Drag / Slider Solver (Canvas Fit & Horizontal / 2D Sliders)
# =====================================================================

def solve_canvas_slider(
    page: Page, 
    canvas_locator: Locator, 
    slider_knob_locator: Optional[Locator] = None, 
    instruction: str = "Click the center of the missing target slot where the piece fits",
    initial_piece_x_offset: float = 93.0,
    initial_piece_y_offset: float = 185.0
) -> bool:
    """
    Computes delta movement from initial piece location to target slot,
    accounting for DPI scaling and human-like bezier/jitter movement.
    """
    solver = get_solver()
    if solver is None:
        _no_key_message("[2Captcha Slider]")
        return False

    try:
        canvas_bytes = canvas_locator.screenshot()
        canvas_box = canvas_locator.bounding_box()
        if not canvas_box:
            return False

        with Image.open(BytesIO(canvas_bytes)) as pil_img:
            img_width, img_height = pil_img.size
        scale_x = canvas_box["width"] / img_width
        scale_y = canvas_box["height"] / img_height

        b64_canvas = base64.b64encode(canvas_bytes).decode("utf-8")
        print(f"[2Captcha] Requesting target coordinate for drag puzzle (DPI Scale: {scale_x:.2f}x, {scale_y:.2f}y)...")
        
        result = solver.coordinates(
            b64_canvas,
            hintText=instruction,
            min_clicks=1,
            max_clicks=2
        )
        print(f"[2Captcha] Raw response: {result}")
        
        raw_code = result.get("code") or result.get("request") or []
        points = parse_coordinates(raw_code)
        if not points:
            return False

        if slider_knob_locator and slider_knob_locator.count() > 0 and slider_knob_locator.is_visible():
            # 1D Horizontal slider
            target_canvas_x = float(points[0]["x"]) * scale_x
            drag_distance_x = target_canvas_x - (initial_piece_x_offset * scale_x)

            knob_box = slider_knob_locator.bounding_box()
            if not knob_box:
                return False

            start_x = knob_box["x"] + (knob_box["width"] / 2)
            start_y = knob_box["y"] + (knob_box["height"] / 2)
            target_x = start_x + drag_distance_x
            target_y = start_y
            drag_distance_y = 0.0
        else:
            # 2D Canvas Drag and Drop (hCaptcha letter/puzzle on canvas)
            # Filter out any clicks on the initial piece area
            target_pt = None
            for pt in points:
                if (float(pt["x"]) * scale_x) > 130 or (float(pt["y"]) * scale_y) > 210:
                    target_pt = pt
                    break
            
            if not target_pt:
                print(f"[2Captcha Slider] Worker only clicked initial tile ({points}), skipping invalid drag.")
                return False

            target_canvas_x = float(target_pt["x"]) * scale_x
            target_canvas_y = float(target_pt["y"]) * scale_y

            # In hCaptcha canvas, handle center in CSS pixels:
            # start handle: (93.0 * scale_x, 140.0 * scale_y)
            # piece center: (93.0 * scale_x, 185.0 * scale_y)
            start_x = canvas_box["x"] + (93.0 * scale_x)
            start_y = canvas_box["y"] + (140.0 * scale_y)

            # Delta displacement to align piece center with target outline:
            delta_x = target_canvas_x - (93.0 * scale_x)
            delta_y = target_canvas_y - (185.0 * scale_y)

            target_x = start_x + delta_x
            target_y = start_y + delta_y
            drag_distance_x = delta_x
            drag_distance_y = delta_y

        print(f"[2Captcha Slider] Dragging from ({start_x:.1f}, {start_y:.1f}) to ({target_x:.1f}, {target_y:.1f}) "
              f"Δx={drag_distance_x:.1f}, Δy={drag_distance_y:.1f}...")

        # Natural drag movement with quadratic ease-out and human micro-variations
        page.mouse.move(start_x, start_y)
        page.wait_for_timeout(random.randint(100, 180))
        page.mouse.down()
        page.wait_for_timeout(random.randint(150, 250))

        steps = 35
        for i in range(1, steps + 1):
            t = i / steps
            # Quadratic ease-out progression
            ease = 1 - (1 - t) * (1 - t)
            current_x = start_x + (drag_distance_x * ease)
            current_y = start_y + (drag_distance_y * ease)

            # Slight random jitter to defeat strict straight-line / constant-velocity heuristics
            if i < steps:
                jitter_x = random.uniform(-1.0, 1.0)
                jitter_y = random.uniform(-1.2, 1.2)
                page.mouse.move(current_x + jitter_x, current_y + jitter_y)
            else:
                page.mouse.move(target_x, target_y)

            time.sleep(random.uniform(0.008, 0.018))

        page.wait_for_timeout(random.randint(200, 350))
        page.mouse.up()
        page.wait_for_timeout(1500)

        # Check if button changed to Verify / Next
        for f in page.frames:
            submit_btn = f.locator('div[class*="button-submit"], button:has-text("Verify"), button:has-text("Next")').first
            if submit_btn.count() > 0 and submit_btn.is_visible():
                try:
                    btn_text = submit_btn.inner_text().strip().lower()
                    print(f"[2Captcha Slider] Challenge button text: '{btn_text}'")
                    if "verify" in btn_text or "next" in btn_text:
                        submit_btn.click(force=True)
                        print(f"[2Captcha Slider] Clicked {btn_text.upper()} button.")
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass

        return True

    except Exception as e:
        print(f"[2Captcha Slider Error]: {e}")
        return False