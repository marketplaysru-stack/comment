#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-комментатор для ВКонтакте.
Отвечает на комментарии в группах, используя Hugging Face, Agnes или шаблоны.
Игнорирует комментарии с запрещёнными словами и от указанных пользователей.
"""

import os
import re
import time
import json
import random
import logging
import requests
import threading
from datetime import datetime
from dotenv import load_dotenv
import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType

load_dotenv()

# ---------- НАСТРОЙКИ ----------
VK_TOKEN_COMMENT = os.getenv("VK_TOKEN_COMMENT", os.getenv("VK_TOKEN_AI"))
if not VK_TOKEN_COMMENT:
    raise ValueError("VK_TOKEN_COMMENT не задан!")

# Список групп для мониторинга (оставлен в коде)
GROUPS_TO_WATCH = [
    -240273450,   # AI Навигатор
    -239598146,   # Строительный навигатор
    240643919,    # Родительский навигатор
    240656847,    # Музыкальный навигатор
    -240683592    # Загородный навигатор
]

# ID пользователей, чьи комментарии игнорировать (ваши боты и вы сами)
IGNORE_USER_IDS = [317272476]  # ваш ID

# Список запрещённых слов (комментарии с ними будут пропущены)
FORBIDDEN_WORDS = ["реклама", "спам", "http", "https", "купить", "скидка", "продвижение", "секс", "порно"]

HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY")
AGNES_API_KEY = os.getenv("AGNES_API_KEY")

DATA_DIR = os.getenv("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "comment_state.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("comment_bot")

# ---------- СОСТОЯНИЕ ----------
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"last_processed": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
def generate_huggingface_response(comment_text):
    """Генерирует ответ через Hugging Face"""
    if not HUGGINGFACE_API_KEY:
        return None
    try:
        model = "microsoft/DialoGPT-medium"
        api_url = f"https://api-inference.huggingface.co/models/{model}"
        headers = {"Authorization": f"Bearer {HUGGINGFACE_API_KEY}"}
        payload = {
            "inputs": comment_text,
            "parameters": {
                "max_length": 100,
                "temperature": 0.7,
                "do_sample": True,
                "pad_token_id": 0
            }
        }
        resp = requests.post(api_url, headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            if isinstance(result, list) and len(result) > 0:
                generated = result[0].get("generated_text", "")
                if generated.startswith(comment_text):
                    generated = generated[len(comment_text):].strip()
                if generated and len(generated) > 3:
                    return generated
            elif isinstance(result, dict) and "generated_text" in result:
                generated = result["generated_text"]
                if generated.startswith(comment_text):
                    generated = generated[len(comment_text):].strip()
                if generated and len(generated) > 3:
                    return generated
        elif resp.status_code == 503:
            logger.warning("Hugging Face: модель загружается, попробуем позже")
        else:
            logger.warning(f"Hugging Face: статус {resp.status_code} - {resp.text[:100]}")
    except Exception as e:
        logger.warning(f"Hugging Face исключение: {e}")
    return None

def generate_agnes_response(comment_text):
    """Генерирует ответ через Agnes"""
    if not AGNES_API_KEY:
        return None
    try:
        headers = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}
        prompt = f"Ты — дружелюбный и профессиональный ассистент сообщества. Ответь на комментарий пользователя в нашей группе. Ответ должен быть вежливым, по делу, помогать или поддерживать. Комментарий: \"{comment_text}\""
        data = {
            "model": "agnes-v1",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 150,
            "temperature": 0.7
        }
        resp = requests.post("https://apihub.agnes-ai.cn/v1/chat/completions", json=data, headers=headers, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            if text and len(text) > 5:
                return text.strip()
    except Exception as e:
        logger.warning(f"Agnes не сработал: {e}")
    return None

def template_response(comment_text):
    """Резервные шаблонные ответы"""
    comment_lower = comment_text.lower()
    if any(w in comment_lower for w in ["спасиб", "благодар"]):
        return "Пожалуйста! Рады, что вам полезно. Обращайтесь ещё 😊"
    if any(w in comment_lower for w in ["крут", "класс", "супер", "отличн"]):
        return "Спасибо за отзыв! Мы стараемся для вас 💪"
    if any(w in comment_lower for w in ["вопрос", "как", "почему", "что"]):
        return "Отличный вопрос! Если нужен более подробный ответ, напишите в сообщения сообщества — мы поможем 📩"
    if any(w in comment_lower for w in ["не понял", "непонятно", "объясни", "разъясни"]):
        return "Постараемся объяснить понятнее. Какой момент вызывает затруднение? 🤔"
    if any(w in comment_lower for w in ["ошибк", "баг", "глюк"]):
        return "Спасибо за сигнал! Мы проверим и исправим. Если есть подробности, напишите в личные сообщения 🛠️"
    return "Благодарим за ваш комментарий! Мы ценим ваше мнение и всегда рады обратной связи 😊"

def generate_response(comment_text, group_id):
    """Основная функция генерации ответа"""
    # 1. Hugging Face
    if HUGGINGFACE_API_KEY:
        response = generate_huggingface_response(comment_text)
        if response:
            return response

    # 2. Agnes
    response = generate_agnes_response(comment_text)
    if response:
        return response

    # 3. Шаблоны
    return template_response(comment_text)

# ---------- ОБРАБОТКА СОБЫТИЙ ----------
def handle_comment(vk, event):
    if event.user_id in IGNORE_USER_IDS:
        return

    comment_text = event.text

    # Проверка на запрещённые слова
    if any(word in comment_text.lower() for word in FORBIDDEN_WORDS):
        logger.info(f"Комментарий содержит запрещённое слово, пропускаем: {comment_text[:50]}...")
        return

    group_id = event.owner_id
    post_id = event.post_id
    comment_id = event.comment_id

    state = load_state()
    key = f"{group_id}_{comment_id}"
    if state.get("last_processed", {}).get(key):
        return

    logger.info(f"💬 Новый комментарий в группе {group_id}: {comment_text[:50]}...")

    answer = generate_response(comment_text, group_id)

    try:
        vk.method('wall.createComment', {
            'owner_id': group_id,
            'post_id': post_id,
            'reply_to_comment': comment_id,
            'message': answer
        })
        logger.info(f"✅ Ответ отправлен: {answer[:50]}...")
        state["last_processed"][key] = {"time": datetime.now().isoformat(), "comment": comment_text[:100]}
        save_state(state)
    except Exception as e:
        logger.error(f"❌ Ошибка отправки ответа: {e}")

# ---------- ЗАПУСК ----------
def main():
    logger.info("🚀 Бот-комментатор запущен (с фильтром запрещённых слов)")
    vk_session = vk_api.VkApi(token=VK_TOKEN_COMMENT)
    vk = vk_session.get_api()

    for gid in GROUPS_TO_WATCH:
        try:
            vk.wall.get(owner_id=gid, count=1)
            logger.info(f"✅ Доступ к группе {gid} подтверждён")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка доступа к группе {gid}: {e}")

    longpoll = VkLongPoll(vk_session, wait=25)

    for event in longpoll.listen():
        if event.type == VkEventType.WALL_REPLY_NEW:
            if event.owner_id in GROUPS_TO_WATCH:
                threading.Thread(target=handle_comment, args=(vk, event), daemon=True).start()

if __name__ == "__main__":
    main()