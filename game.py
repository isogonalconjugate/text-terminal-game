#!/usr/bin/env python3
"""
Текстовая RPG с движком на JSON, сохранениями, полосками здоровья,
прокручиваемым инвентарём (с переносом строк) и цветовыми индикаторами действий.
"""

import json
import curses
import sys
import os
from typing import Dict, List, Any, Optional, Set, Tuple
from datetime import datetime

# ---------- Структуры данных ----------

class Action:
    """Действие, доступное игроку."""
    def __init__(self, data: Dict[str, Any], action_id: Optional[str] = None):
        self.id = action_id
        self.text = data.get("text", "")
        self.next = data.get("next", "")
        self.conditions = data.get("conditions", {})
        self.effects = data.get("effects", {})
        self.once = data.get("once", False)
        self.repeat_text = data.get("repeat_text", None)
        self.repeat_effects = data.get("repeat_effects", {})
        self.repeat_next = data.get("repeat_next", None)
        self.death_next = data.get("death_next", None)  # специальный узел смерти

class Node:
    """Узел сцены."""
    def __init__(self, data: Dict[str, Any]):
        self.id = data.get("id", "")
        self.text = data.get("text", "")
        self.image = data.get("image", "")
        self.stats = data.get("stats")
        self.actions = [Action(a, f"{self.id}_{i}") for i, a in enumerate(data.get("actions", []))]

# ---------- Система сохранений ----------

class SaveGame:
    """Управление сохранениями."""
    
    def __init__(self, save_file: str):
        self.save_file = save_file
        self.data = {
            "current_node": "start",
            "stats": {},
            "completed_actions": set(),
            "visited_nodes": set(),
            "last_saved": None
        }
    
    def load(self) -> bool:
        if not os.path.exists(self.save_file):
            return False
        try:
            with open(self.save_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                self.data["current_node"] = loaded.get("current_node", "start")
                self.data["stats"] = loaded.get("stats", {})
                self.data["completed_actions"] = set(loaded.get("completed_actions", []))
                self.data["visited_nodes"] = set(loaded.get("visited_nodes", []))
                self.data["last_saved"] = loaded.get("last_saved")
            return True
        except Exception as e:
            print(f"Ошибка загрузки сохранения: {e}")
            return False
    
    def save(self) -> None:
        self.data["last_saved"] = datetime.now().isoformat()
        save_data = {
            "current_node": self.data["current_node"],
            "stats": self.data["stats"],
            "completed_actions": list(self.data["completed_actions"]),
            "visited_nodes": list(self.data["visited_nodes"]),
            "last_saved": self.data["last_saved"]
        }
        with open(self.save_file, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
    
    def mark_action_completed(self, action_id: str) -> None:
        self.data["completed_actions"].add(action_id)
    
    def is_action_completed(self, action_id: str) -> bool:
        return action_id in self.data["completed_actions"]
    
    def mark_node_visited(self, node_id: str) -> None:
        self.data["visited_nodes"].add(node_id)
    
    def update_stats(self, stats: Dict[str, Any]) -> None:
        self.data["stats"] = stats.copy()
    
    def set_current_node(self, node_id: str) -> None:
        self.data["current_node"] = node_id
    
    def reset_to_new_game(self, start_node_stats: Optional[Dict[str, Any]] = None) -> None:
        """Сбрасывает сохранение для новой игры."""
        self.data = {
            "current_node": "start",
            "stats": start_node_stats.copy() if start_node_stats else {},
            "completed_actions": set(),
            "visited_nodes": set(),
            "last_saved": None
        }

# ---------- Движок игры ----------

class GameEngine:
    def __init__(self, data_file: str):
        # Загрузка игровых данных с обработкой ошибок
        try:
            with open(data_file, 'r', encoding='utf-8') as f:
                raw_data = json.load(f)
        except FileNotFoundError:
            print(f"Ошибка: файл {data_file} не найден.")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Ошибка: файл {data_file} содержит неверный JSON.")
            print(f"Детали: {e}")
            sys.exit(1)
        
        self.nodes: Dict[str, Node] = {}
        for node_id, node_data in raw_data.items():
            if "id" not in node_data:
                node_data["id"] = node_id
            self.nodes[node_id] = Node(node_data)
        
        self.save_file = None
        self.save = None
        self.stats: Dict[str, Any] = {}
        self.current_node_id: Optional[str] = None
        self.loaded_save = False
        self.inv_scroll_offset = 0  # смещение для прокрутки инвентаря
    
    # ---------- Вспомогательные функции для отображения ----------
    
    def _wrap_text(self, text: str, width: int) -> List[str]:
        """Переносит текст на строки заданной ширины (слова не разбиваются)."""
        if not text:
            return []
        words = text.split()
        lines = []
        current_line = []
        current_len = 0
        for w in words:
            if current_len + len(w) + 1 <= width:
                current_line.append(w)
                current_len += len(w) + 1
            else:
                lines.append(' '.join(current_line))
                current_line = [w]
                current_len = len(w) + 1
        if current_line:
            lines.append(' '.join(current_line))
        return lines
    
    def _wrap_items(self, items: List[str], prefix: str, max_width: int) -> List[str]:
        """Формирует строки инвентаря с переносом по словам."""
        lines = []
        current_line = prefix
        for item in items:
            test_line = current_line + item + ", "
            if len(test_line) <= max_width:
                current_line = test_line
            else:
                if current_line != prefix:
                    lines.append(current_line.rstrip(", "))
                current_line = "          " + item + ", "
        if current_line != prefix:
            lines.append(current_line.rstrip(", "))
        return lines
    
    # ---------- Методы для панели и полосок ----------
    
    def draw_health_bar(self, stdscr, y, x, current, max_value=20, width=15):
        """Рисует полоску здоровья."""
        filled = int(width * current / max_value) if max_value > 0 else 0
        filled = min(filled, width)
        bar = "█" * filled + "░" * (width - filled)
        stdscr.addstr(y, x, bar)
    
    def draw_stat_panel(self, stdscr, y, x, stats):
        """Рисует панель со статами и прокручиваемым инвентарём (с переносом строк)."""
        health = stats.get('health', 0)
        strength = stats.get('strength', 0)
        money = stats.get('money', 0)
        max_health = stats.get('max_health', 20)
        
        # Полоска здоровья
        width = 12
        filled = int(width * health / max_health) if max_health > 0 else 0
        filled = min(filled, width)
        bar = "█" * filled + "░" * (width - filled)
        
        # Первая строка: здоровье, сила, золото
        line1 = f"Здоровье: {health:2}/{max_health:2} [{bar}]  Сила: {strength:2}  Золото: {money:4}"
        
        # Формируем строки инвентаря
        lines = [line1]
        
        if 'inventory' in stats and stats['inventory']:
            inv_items = stats['inventory']
            total_items = len(inv_items)
            
            max_visible = 6
            max_offset = max(0, total_items - max_visible)
            self.inv_scroll_offset = max(0, min(self.inv_scroll_offset, max_offset))
            
            visible_items = inv_items[self.inv_scroll_offset:self.inv_scroll_offset + max_visible]
            max_content_width = 70
            inventory_lines = self._wrap_items(visible_items, "Предметы: ", max_content_width)
            lines.extend(inventory_lines)
            
            if total_items > max_visible:
                scroll_indicator = ""
                if self.inv_scroll_offset > 0 and self.inv_scroll_offset + max_visible < total_items:
                    scroll_indicator = " ◀▶ "
                elif self.inv_scroll_offset > 0:
                    scroll_indicator = " ◀ "
                else:
                    scroll_indicator = " ▶ "
                lines.append("          [←/→] листать инвентарь" + scroll_indicator)
        else:
            lines.append("Предметы: (пусто)")
        
        # Вычисляем максимальную длину строки для рамки
        max_len = max(len(l) for l in lines) + 2
        frame_width = max_len + 2
        
        # Рисуем рамку
        stdscr.addstr(y, x, "╔" + "═" * frame_width + "╗")
        for i, line in enumerate(lines):
            padded_line = " " + line + " "
            stdscr.addstr(y + 1 + i, x, "║" + padded_line.ljust(frame_width) + "║")
        stdscr.addstr(y + 1 + len(lines), x, "╚" + "═" * frame_width + "╝")
        # Подсказки под панелью
        stdscr.addstr(y + 2 + len(lines), x, "  [↑/↓] выбор  [Enter] действие  [S] сохранить  [Q] выход")
    
    # ---------- Логика условий и эффектов ----------
    
    def evaluate_condition(self, condition: Dict[str, Any], stats: Dict[str, Any]) -> bool:
        for key, value in condition.items():
            if key == "inventory_has":
                if value not in stats.get("inventory", []):
                    return False
            else:
                if isinstance(value, dict):
                    op, val = next(iter(value.items()))
                    current = stats.get(key, 0)
                    if op == ">=" and current < val:
                        return False
                    elif op == "<=" and current > val:
                        return False
                    elif op == "==" and current != val:
                        return False
                    elif op == ">" and current <= val:
                        return False
                    elif op == "<" and current >= val:
                        return False
                else:
                    if stats.get(key) != value:
                        return False
        return True

    def apply_effects(self, effects: Dict[str, Any], stats: Dict[str, Any]) -> None:
        for key, value in effects.items():
            if key == "inventory_add":
                stats.setdefault("inventory", []).extend(value)
            elif key == "inventory_remove":
                inv = stats.get("inventory", [])
                for item in value:
                    if item in inv:
                        inv.remove(item)
            else:
                if key not in stats:
                    stats[key] = 0
                stats[key] += value

    def get_available_actions(self, node: Node) -> List[Dict]:
        available = []
        for action in node.actions:
            is_completed = action.once and self.save.is_action_completed(action.id)
            conditions_met = self.evaluate_condition(action.conditions, self.stats)
            
            if is_completed and action.repeat_text:
                display_text = action.repeat_text
            else:
                display_text = action.text
            
            if not conditions_met:
                display_text = f"[✗] {display_text}"
            else:
                display_text = f"[✓] {display_text}"
            
            available.append({
                "action": action,
                "is_new": not is_completed,
                "display_text": display_text,
                "is_completed": is_completed,
                "available": conditions_met
            })
        return available

    # ---------- Меню сохранений и окончания ----------
    
    def show_save_slot_menu(self, stdscr, title="ВЫБОР СЛОТА ДЛЯ СОХРАНЕНИЯ") -> Optional[str]:
        """Меню выбора слота для сохранения с динамической рамкой."""
        h, w = stdscr.getmaxyx()
        
        saves = []
        for i in range(1, 4):
            save_file = f"save_{i}.json"
            if os.path.exists(save_file):
                try:
                    with open(save_file, 'r') as f:
                        data = json.load(f)
                        last_saved = data.get("last_saved", "Unknown")
                        node = data.get("current_node", "start")
                        short_date = last_saved.split('T')[0] if 'T' in last_saved else last_saved[:16]
                        saves.append((save_file, f"Слот {i} - {short_date} - {node}", "exists"))
                except:
                    saves.append((save_file, f"Слот {i} - Повреждено", "corrupted"))
            else:
                saves.append((save_file, f"Слот {i} - Пусто", "empty"))
        
        options = [f"{desc}" for _, desc, _ in saves]
        options.append("Отмена")
        
        max_len = max(len(opt) for opt in options)
        max_len = max(max_len, len(title))
        frame_width = max_len + 4
        
        current = 0
        while True:
            stdscr.clear()
            left = w//2 - frame_width//2
            stdscr.addstr(h//2 - 3, left, "╔" + "═" * frame_width + "╗")
            stdscr.addstr(h//2 - 2, left, "║" + title.center(frame_width) + "║")
            stdscr.addstr(h//2 - 1, left, "╠" + "═" * frame_width + "╣")
            
            for idx, opt in enumerate(options):
                y = h//2 + idx
                padded = opt.ljust(frame_width)
                if idx == current:
                    stdscr.attron(curses.A_REVERSE)
                    stdscr.addstr(y, left, "║" + padded + "║")
                    stdscr.attroff(curses.A_REVERSE)
                else:
                    stdscr.addstr(y, left, "║" + padded + "║")
            
            stdscr.addstr(h//2 + len(options), left, "╚" + "═" * frame_width + "╝")
            stdscr.addstr(h-2, 2, "[↑/↓] выбор  [Enter] подтвердить")
            
            key = stdscr.getch()
            if key == curses.KEY_UP and current > 0:
                current -= 1
            elif key == curses.KEY_DOWN and current < len(options)-1:
                current += 1
            elif key == ord('\n'):
                if current == len(options)-1:
                    return None
                else:
                    return saves[current][0]
    
    def show_save_menu(self, stdscr) -> bool:
        """Меню сохранения с выбором слота."""
        save_file = self.show_save_slot_menu(stdscr, "ВЫБОР СЛОТА ДЛЯ СОХРАНЕНИЯ")
        if not save_file:
            return True
        
        self.save = SaveGame(save_file)
        self.save.update_stats(self.stats)
        self.save.set_current_node(self.current_node_id)
        self.save.save()
        
        h, w = stdscr.getmaxyx()
        stdscr.clear()
        stdscr.addstr(h//2, w//2 - 10, "Сохранено!")
        stdscr.refresh()
        curses.napms(1000)
        return True
    
    def show_load_menu(self, stdscr) -> Optional[Tuple[str, bool]]:
        """Меню загрузки с динамической рамкой."""
        h, w = stdscr.getmaxyx()
        saves = []
        for i in range(1, 4):
            save_file = f"save_{i}.json"
            if os.path.exists(save_file):
                try:
                    with open(save_file, 'r') as f:
                        data = json.load(f)
                        last_saved = data.get("last_saved", "Unknown")
                        node = data.get("current_node", "start")
                        short_date = last_saved.split('T')[0] if 'T' in last_saved else last_saved[:16]
                        saves.append((save_file, f"Слот {i} - {short_date} - {node}", "exists"))
                except:
                    saves.append((save_file, f"Слот {i} - Повреждено", "corrupted"))
            else:
                saves.append((save_file, f"Слот {i} - Пусто", "empty"))
        
        options = [f"{desc}" for _, desc, _ in saves]
        options.append("Новая игра (выбрать слот)")
        
        title = "ЗАГРУЗКА ИГРЫ"
        max_len = max(len(opt) for opt in options)
        max_len = max(max_len, len(title))
        frame_width = max_len + 4
        
        current = 0
        while True:
            stdscr.clear()
            left = w//2 - frame_width//2
            stdscr.addstr(h//2 - 3, left, "╔" + "═" * frame_width + "╗")
            stdscr.addstr(h//2 - 2, left, "║" + title.center(frame_width) + "║")
            stdscr.addstr(h//2 - 1, left, "╠" + "═" * frame_width + "╣")
            
            for idx, opt in enumerate(options):
                y = h//2 + idx
                padded = opt.ljust(frame_width)
                if idx == current:
                    stdscr.attron(curses.A_REVERSE)
                    stdscr.addstr(y, left, "║" + padded + "║")
                    stdscr.attroff(curses.A_REVERSE)
                else:
                    stdscr.addstr(y, left, "║" + padded + "║")
            
            stdscr.addstr(h//2 + len(options), left, "╚" + "═" * frame_width + "╝")
            stdscr.addstr(h-2, 2, "[↑/↓] выбор  [Enter] подтвердить")
            
            key = stdscr.getch()
            if key == curses.KEY_UP and current > 0:
                current -= 1
            elif key == curses.KEY_DOWN and current < len(options)-1:
                current += 1
            elif key == ord('\n'):
                if current == len(options)-1:
                    slot = self.show_save_slot_menu(stdscr, "ВЫБОР СЛОТА ДЛЯ НОВОЙ ИГРЫ")
                    if slot:
                        return (slot, True)
                    else:
                        continue
                elif saves[current][2] == "exists":
                    return (saves[current][0], False)
                else:
                    return (saves[current][0], True)
    
    def show_end_game_screen(self, stdscr, node) -> bool:
        h, w = stdscr.getmaxyx()
        stdscr.clear()
        text_lines = self._wrap_text(node.text, w-4)
        start_y = h//2 - len(text_lines)//2
        for i, line in enumerate(text_lines):
            stdscr.addstr(start_y + i, w//2 - len(line)//2, line)
        
        options = ["Начать заново", "Выход"]
        current = 0
        while True:
            for idx, opt in enumerate(options):
                y = start_y + len(text_lines) + 2 + idx
                x = w//2 - len(opt)//2
                if idx == current:
                    stdscr.attron(curses.A_REVERSE)
                    stdscr.addstr(y, x, opt)
                    stdscr.attroff(curses.A_REVERSE)
                else:
                    stdscr.addstr(y, x, opt)
            key = stdscr.getch()
            if key == curses.KEY_UP and current > 0:
                current -= 1
            elif key == curses.KEY_DOWN and current < len(options)-1:
                current += 1
            elif key == ord('\n'):
                if current == 0:
                    slot = self.show_save_slot_menu(stdscr, "ВЫБОР СЛОТА ДЛЯ НОВОЙ ИГРЫ")
                    if slot:
                        self.save = SaveGame(slot)
                        start_node = self.nodes.get("start")
                        if start_node and start_node.stats:
                            self.stats = start_node.stats.copy()
                        else:
                            self.stats = {}
                        self.save.reset_to_new_game(self.stats)
                        self.save.save()
                        self.current_node_id = "start"
                        self.save_file = slot
                        self.loaded_save = True
                        self.inv_scroll_offset = 0
                        return True
                    else:
                        continue
                else:
                    return False
        return True

    # ---------- Основной цикл ----------
    
    def wrap_text(self, text: str, width: int) -> List[str]:
        return self._wrap_text(text, width)

    def start(self, start_id: str, save_file: str = None):
        if save_file:
            self.save_file = save_file
            self.save = SaveGame(self.save_file)
            self.loaded_save = self.save.load()
            if self.loaded_save:
                self.current_node_id = self.save.data["current_node"]
                self.stats = self.save.data["stats"].copy()
            else:
                self.current_node_id = start_id
                start_node = self.nodes.get(start_id)
                if start_node and start_node.stats:
                    self.stats = start_node.stats.copy()
                    self.save.update_stats(self.stats)
            curses.wrapper(self.main_loop)
        else:
            curses.wrapper(self.show_load_menu_and_start, start_id)
    
    def show_load_menu_and_start(self, stdscr, start_id):
        result = self.show_load_menu(stdscr)
        if result:
            save_file, overwrite = result
            self.save_file = save_file
            self.save = SaveGame(self.save_file)
            
            if overwrite:
                start_node = self.nodes.get(start_id)
                if start_node and start_node.stats:
                    self.stats = start_node.stats.copy()
                else:
                    self.stats = {}
                self.save.reset_to_new_game(self.stats)
                self.save.save()
                self.current_node_id = start_id
                self.loaded_save = True
            else:
                self.loaded_save = self.save.load()
                if self.loaded_save:
                    self.current_node_id = self.save.data["current_node"]
                    self.stats = self.save.data["stats"].copy()
                else:
                    start_node = self.nodes.get(start_id)
                    if start_node and start_node.stats:
                        self.stats = start_node.stats.copy()
                    self.save.update_stats(self.stats)
                    self.current_node_id = start_id
            
            self.inv_scroll_offset = 0
            self.main_loop(stdscr)
    
    def main_loop(self, stdscr):
        curses.curs_set(0)
        
        if curses.has_colors():
            curses.start_color()
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
            curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)
            curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)
        
        h, w = stdscr.getmaxyx()
        if h < 20 or w < 60:
            stdscr.clear()
            stdscr.addstr(0, 0, "Окно терминала слишком маленькое.")
            stdscr.addstr(1, 0, f"Текущий размер: {w}x{h}. Минимальный: 60x20")
            stdscr.addstr(2, 0, "Измените размер окна и перезапустите игру.")
            stdscr.refresh()
            stdscr.getch()
            return
        
        while True:
            node = self.nodes.get(self.current_node_id)
            if not node:
                break
            
            self.save.mark_node_visited(node.id)
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            
            if node.image:
                art_lines = node.image.split('\n')
                for i, line in enumerate(art_lines):
                    if i < h - 15:
                        try:
                            stdscr.addstr(i, 0, line[:w-1])
                        except:
                            pass
            
            text_lines = self.wrap_text(node.text, w-4)
            start_y = len(node.image.split('\n')) + 1 if node.image else 1
            for i, line in enumerate(text_lines):
                if start_y + i >= h - 12:
                    break
                try:
                    stdscr.addstr(start_y + i, 2, line)
                except:
                    pass
            
            available_actions = self.get_available_actions(node)
            if not available_actions:
                if not self.show_end_game_screen(stdscr, node):
                    return
                else:
                    continue
            
            if 'max_health' not in self.stats:
                self.stats['max_health'] = 20
            
            panel_y = h - 15
            self.draw_stat_panel(stdscr, panel_y, 1, self.stats)
            
            menu_y = start_y + len(text_lines) + 2
            options = [a["display_text"] for a in available_actions]
            current = 0
            
            while True:
                for idx, opt in enumerate(options):
                    y = menu_y + idx
                    if y >= panel_y - 1:
                        break
                    action_info = available_actions[idx]
                    is_available = action_info["available"]
                    
                    if idx == current:
                        stdscr.attron(curses.A_REVERSE)
                        try:
                            stdscr.addstr(y, 2, opt[:w-4])
                        except:
                            pass
                        stdscr.attroff(curses.A_REVERSE)
                    else:
                        if not is_available and curses.has_colors():
                            stdscr.attron(curses.color_pair(2))
                        elif curses.has_colors():
                            stdscr.attron(curses.color_pair(1))
                        try:
                            stdscr.addstr(y, 2, opt[:w-4])
                        except:
                            pass
                        if curses.has_colors():
                            stdscr.attroff(curses.color_pair(2))
                            stdscr.attroff(curses.color_pair(1))
                
                key = stdscr.getch()
                if key == curses.KEY_LEFT:
                    self.inv_scroll_offset = max(0, self.inv_scroll_offset - 3)
                    break
                elif key == curses.KEY_RIGHT:
                    total_items = len(self.stats.get('inventory', []))
                    max_visible = 6
                    max_offset = max(0, total_items - max_visible)
                    self.inv_scroll_offset = min(max_offset, self.inv_scroll_offset + 3)
                    break
                elif key == curses.KEY_UP and current > 0:
                    current -= 1
                elif key == curses.KEY_DOWN and current < len(options)-1:
                    current += 1
                elif key == ord('\n'):
                    action_info = available_actions[current]
                    action = action_info["action"]
                    is_new = action_info["is_new"]
                    is_available = action_info["available"]
                    if not is_available:
                        continue
                    
                    if is_new:
                        effects_to_apply = action.effects
                        next_node = action.next
                    else:
                        effects_to_apply = action.repeat_effects if action.repeat_effects else {}
                        next_node = action.repeat_next if action.repeat_next else action.next
                    
                    self.apply_effects(effects_to_apply, self.stats)
                    if action.once and is_new:
                        self.save.mark_action_completed(action.id)
                    
                    self.save.update_stats(self.stats)
                    
                    # Проверка смерти после применения эффектов
                    if self.stats.get('health', 0) <= 0:
                        # Если есть специальный узел смерти для этого действия, идём туда
                        if action.death_next:
                            self.current_node_id = action.death_next
                        else:
                            death_node = self.nodes.get("death")
                            if death_node:
                                self.current_node_id = "death"
                            else:
                                break
                        self.save.set_current_node(self.current_node_id)
                        self.save.save()
                        break  # выход из внутреннего цикла для перерисовки с узлом смерти
                    else:
                        self.current_node_id = next_node
                        self.save.set_current_node(self.current_node_id)
                        self.save.save()
                        break
                    
                elif key == ord('s') or key == ord('S'):
                    if not self.show_save_menu(stdscr):
                        return
                    break
                    
                elif key == ord('q') or key == ord('Q'):
                    if self.show_save_menu(stdscr) is False:
                        return
                    else:
                        break

# ---------- Точка входа ----------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python game.py <файл_сценария.json> [файл_сохранения.json]")
        print("  файл_сохранения.json - опционально, если не указан - покажет меню выбора")
        sys.exit(1)
    
    save_file = sys.argv[2] if len(sys.argv) > 2 else None
    game = GameEngine(sys.argv[1])
    game.start("start", save_file)