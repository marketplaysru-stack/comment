#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-комментатор для ВКонтакте (рабочая версия).
Использует groups.getLongPollServer для получения событий групп.
Отвечает на комментарии через Hugging Face, Agnes или шаблоны.
"""

import os
import time
import json
import logging
import random
import requests
import threading
from datetime import datetime
from dotenv import load_dotenv
import vk_api

load_dotenv()

# ---------- НАСТРОЙКИ ----------
VK_TOKEN_COMMENT = os.getenv("VK_TOKEN_COMMENT", os.getenv("VK_TOKEN_AI"))
if not VK_TOKEN_COMMENT:
    raise ValueError("VK_TOKEN_COMMENT не задан!")

# Группы для мониторинга
GROUPS_TO_WATCH = [
    -240273450,   # AI Навигатор
    -239598146,   # Строительный навигатор
    240643919,    # Родительский навигатор
    240656847,    # Музыкальный навигатор
    -240683592    # Загородный навигатор
]

# ID пользователей, чьи комментарии игнорировать
IGNORE_USER_IDS = [317272476]

# Запрещённые слова
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
        return {"last_processed": {}, "ts": 0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ---------- ГЕНЕРАЦИЯ ОТВЕТА ----------
def generate_huggingface_response(comment_text):
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
        elif resp.status_code == 503:
            logger.warning("Hugging Face: модель загружается")
    except Exception as e:
        logger.warning(f"Hugging Face ошибка: {e}")
    return None

def generate_agnes_response(comment_text):
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
        logger.warning(f"Agnes ошибка: {e}")
    return None

def template_response(comment_text):
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
    response = generate_huggingface_response(comment_text)
    if response:
        return response
    response = generate_agnes_response(comment_text)
    if response:
        return response
    return template_response(comment_text)

# ---------- ОБРАБОТКА КОММЕНТАРИЯ ----------
def process_comment(vk, group_id, post_id, comment_id, user_id, comment_text):
    # Проверяем, не от себя ли
    if user_id in IGNORE_USER_IDS:
        return

    # Проверяем запрещённые слова
    if any(word in comment_text.lower() for word in FORBIDDEN_WORDS):
        logger.info(f"Запрещённое слово, пропускаем: {comment_text[:50]}...")
        return

    # Проверяем, не обработан ли уже
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
        state["last_processed"][key] = {
            "time": datetime.now().isoformat(),
            "comment": comment_text[:100]
        }
        save_state(state)
    except Exception as e:
        logger.error(f"❌ Ошибка отправки ответа: {e}")

# ---------- ПОЛУЧЕНИЕ СОБЫТИЙ ГРУППЫ ----------
def get_longpoll_server(vk, group_id):
    """Получает сервер для Long Poll группы"""
    try:
        resp = vk.method('groups.getLongPollServer', {
            'group_id': abs(group_id),
            'lp_version': 3
        })
        return resp.get('server'), resp.get('key'), resp.get('ts')
    except Exception as e:
        logger.error(f"Ошибка получения Long Poll сервера для группы {group_id}: {e}")
        return None, None, None

def listen_group(vk, group_id):
    """Слушает события одной группы через Long Poll"""
    server, key, ts = get_longpoll_server(vk, group_id)
    if not server:
        logger.error(f"Не удалось получить сервер для группы {group_id}")
        return

    logger.info(f"👂 Начинаем слушать группу {group_id}")

    while True:
        try:
            url = f"{server}?act=a_check&key={key}&ts={ts}&wait=25"
            resp = requests.get(url, timeout=30)
            if resp.status_code != 200:
                logger.warning(f"Ошибка Long Poll {group_id}: статус {resp.status_code}")
                time.sleep(5)
                continue

            data = resp.json()
            if 'failed' in data:
                if data['failed'] == 1:
                    logger.warning(f"Обновляем ts для группы {group_id}")
                    ts = data.get('ts', ts)
                else:
                    logger.warning(f"Переподключаемся к группе {group_id}, код: {data['failed']}")
                    server, key, ts = get_longpoll_server(vk, group_id)
                    if not server:
                        time.sleep(10)
                    continue
                continue

            if 'ts' in data:
                ts = data['ts']

            # Обрабатываем события
            for update in data.get('updates', []):
                if update.get('type') == 'wall_reply_new':
                    obj = update.get('object', {})
                    comment = obj.get('comment', {})
                    post_id = comment.get('post_id')
                    comment_id = comment.get('id')
                    user_id = comment.get('from_id')
                    comment_text = comment.get('text', '')

                    if comment_id and comment_text:
                        # Запускаем обработку в отдельном потоке
                        threading.Thread(
                            target=process_comment,
                            args=(vk, group_id, post_id, comment_id, user_id, comment_text),
                            daemon=True
                        ).start()

        except requests.exceptions.Timeout:
            logger.debug(f"Таймаут Long Poll {group_id}, продолжаем...")
        except Exception as e:
            logger.error(f"Ошибка в Long Poll группы {group_id}: {e}")
            time.sleep(5)

# ---------- ЗАПУСК ----------
def main():
    logger.info("🚀 Бот-комментатор запущен (через groups.getLongPollServer)")

    vk_session = vk_api.VkApi(token=VK_TOKEN_COMMENT)
    vk = vk_session.get_api()

    # Проверяем доступ к группам
    for gid in GROUPS_TO_WATCH:
        try:
            vk.wall.get(owner_id=gid, count=1)
            logger.info(f"✅ Доступ к группе {gid} подтверждён")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка доступа к группе {gid}: {e}")

    # Запускаем поток для каждой группы
    for gid in GROUPS_TO_WATCH:
        t = threading.Thread(target=listen_group, args=(vk, gid), daemon=True)
        t.start()

    logger.info("✅ Все потоки запущены, бот слушает комментарии")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Бот остановлен")

if __name__ == "__main__":
    main()