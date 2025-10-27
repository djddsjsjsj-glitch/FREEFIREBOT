import os
import subprocess
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime, timedelta
import zipfile
import shutil
import logging
import sys
import time
import threading
import tempfile
import traceback
import json
from functools import wraps
import hashlib
# Optional Docker import (uncomment if needed)
# import docker

# ==========================
# Advanced Logging Setup
# ==========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ==========================
# Bot and Project Configurations
# ==========================
BOT_TOKEN = "7746349331:AAGLuXCiv21wiLdFpUsvoZ8jodPUP6iqLrU"  # Replace with your bot token
ADMIN_IDS = [7761577562]  # Replace with admin user IDs
PROJECTS_DIR = "projects"
REQUIREMENTS_FILE = "requirements.txt"
AUTO_RESTART_DELAY = 60  # Seconds before auto-restart
ALLOWED_USERS_FILE = "allowed_users.json"
MAX_SCRIPTS = 2  # Maximum scripts to run simultaneously
GLOBAL_RESTART_INTERVAL = 3600  # 10 minutes in seconds
USER_PROJECTS_FILE = "user_projects.json"

# ==========================
# Validate Bot Token
# ==========================
if not BOT_TOKEN or BOT_TOKEN.strip() == "":
    raise ValueError("❌ Invalid or empty bot token! Set your bot token from BotFather.")

# ==========================
# Initialize Bot
# ==========================
bot = telebot.TeleBot(BOT_TOKEN)
logger.info("✅ Bot initialized successfully!")

# Check if running in Docker for debugging
if os.path.exists('/.dockerenv'):
    logger.info("Detected Docker environment")
else:
    logger.info("Running in non-Docker environment")

# ==========================
# Error Handler Decorator
# ==========================
def error_handler(func):
    """Decorator for error handling with retry mechanism"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        retries = 0
        while retries < 3:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {str(e)}\n{traceback.format_exc()}")
                retries += 1
                if retries < 3:
                    logger.info(f"⏳ Retrying {retries}/3 in 5 seconds...")
                    time.sleep(5)
                else:
                    logger.critical(f"💥 Operation failed after 3 retries")
                    raise
    return wrapper

# ==========================
# Safe Message Sending
# ==========================
@error_handler
def send_message_safe(chat_id, text, reply_markup=None):
    bot.send_message(chat_id, text, reply_markup=reply_markup)

logger.info("📌 Bot is ready to run, handlers can be added now")

# ==========================
# User Manager Class
# ==========================
class UserManager:
    """Manages authorized users for the bot"""
    def __init__(self):
        self.allowed_users = set(ADMIN_IDS)
        self.load_allowed_users()
    
    def load_allowed_users(self):
        """Load allowed users from file"""
        try:
            if os.path.exists(ALLOWED_USERS_FILE):
                with open(ALLOWED_USERS_FILE, 'r') as f:
                    data = json.load(f)
                    self.allowed_users.update(data.get('allowed_users', []))
        except Exception as e:
            logger.error(f"Error loading allowed users: {str(e)}")
    
    def save_allowed_users(self):
        """Save allowed users to file"""
        try:
            data = {'allowed_users': list(self.allowed_users)}
            with open(ALLOWED_USERS_FILE, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.error(f"Error saving allowed users: {str(e)}")
    
    def add_user(self, user_id: int):
        """Add a new user"""
        self.allowed_users.add(user_id)
        self.save_allowed_users()
    
    def remove_user(self, user_id: int):
        """Remove a user"""
        if user_id in self.allowed_users and user_id not in ADMIN_IDS:
            self.allowed_users.remove(user_id)
            self.save_allowed_users()
            return True
        return False
    
    def is_allowed(self, user_id: int) -> bool:
        """Check if user is authorized"""
        return user_id in self.allowed_users
    
    def list_users(self):
        """Get list of all users"""
        return list(self.allowed_users)

# ==========================
# Project Manager Class
# ==========================
class ProjectManager:
    """Manages project execution and lifecycle"""
    def __init__(self, user_manager):
        self.running_processes = {}
        self.user_projects = {}
        self.paused_processes = {}
        self.waiting_for_main_file = {}
        self.waiting_for_duration = {}
        self.keep_running = True
        self.global_restart_thread = None
        self.user_manager = user_manager
        self.script_hashes = {}
        
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        self.load_user_projects()
        self.start_global_restart()
    
    def load_user_projects(self):
        """Load user projects from file"""
        try:
            if os.path.exists(USER_PROJECTS_FILE):
                with open(USER_PROJECTS_FILE, 'r') as f:
                    data = json.load(f)
                    self.user_projects = {int(k): v for k, v in data.items()}
                    logger.info(f"Loaded {len(self.user_projects)} user projects")
        except Exception as e:
            logger.error(f"Error loading user projects: {str(e)}")
    
    def save_user_projects(self):
        """Save user projects to file"""
        try:
            with open(USER_PROJECTS_FILE, 'w') as f:
                json.dump(self.user_projects, f)
            logger.info("Saved user projects successfully")
        except Exception as e:
            logger.error(f"Error saving user projects: {str(e)}")
    
    def get_python_scripts(self, project_dir: str) -> list:
        """Get list of all Python files in project directory"""
        python_files = []
        for root, _, files in os.walk(project_dir):
            for file in files:
                if file.endswith('.py'):
                    rel_path = os.path.relpath(os.path.join(root, file), project_dir)
                    python_files.append(rel_path.replace('\\', '/'))
        return python_files
    
    def start_global_restart(self):
        """Start global restart system for all projects every 10 minutes"""
        if self.global_restart_thread and self.global_restart_thread.is_alive():
            return
            
        self.global_restart_thread = threading.Thread(target=self._global_restart_projects)
        self.global_restart_thread.daemon = True
        self.global_restart_thread.start()
    
    def _global_restart_projects(self):
        """Restart all projects periodically"""
        while self.keep_running:
            try:
                time.sleep(GLOBAL_RESTART_INTERVAL)
                
                if not self.running_processes:
                    continue
                
                logger.info("Starting global project restart...")
                
                for project_dir, process_info in list(self.running_processes.items()):
                    chat_id = process_info['chat_id']
                    project_name = process_info['project_name']
                    main_files = process_info['main_files']
                    end_time = process_info.get('end_time')
                    auto_restart = process_info.get('auto_restart', True)
                    
                    if not auto_restart:
                        continue
                    
                    try:
                        self.stop_project(project_dir, chat_id)
                        duration_days = (end_time - datetime.now()).days if end_time else None
                        self.run_project(project_dir, chat_id, duration_days, auto_restart)
                        logger.info(f"Restarted project: {project_name}")
                        bot.send_message(chat_id, f"🔄 Project restarted: {project_name}")
                    except Exception as e:
                        logger.error(f"Failed to restart project {project_name}: {str(e)}")
                        bot.send_message(chat_id, f"❌ Failed to restart project: {project_name}")
                
                logger.info("Global project restart completed")
            except Exception as e:
                logger.error(f"Error in global restart: {str(e)}\n{traceback.format_exc()}")
                time.sleep(30)

    @error_handler
    def install_requirements(self, project_dir: str, chat_id: int) -> bool:
        """Install dependencies from requirements.txt"""
        requirements_path = os.path.join(project_dir, REQUIREMENTS_FILE)
        if os.path.exists(requirements_path):
            try:
                logger.info(f"Installing dependencies for project in {project_dir}")
                process = subprocess.run(
                    ['pip', 'install', '-r', requirements_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=300
                )
                
                if process.returncode != 0:
                    error_msg = f"❌ Failed to install dependencies:\n{process.stderr[:1000]}"
                    bot.send_message(chat_id, error_msg)
                    logger.error(f"Failed to install dependencies: {process.stderr}")
                    return False
                
                logger.info("Dependencies installed successfully")
                return True
            except subprocess.TimeoutExpired:
                error_msg = "❌ Dependency installation timed out (5 minutes)"
                bot.send_message(chat_id, error_msg)
                logger.error("Dependency installation timed out")
                return False
            except Exception as e:
                error_msg = f"❌ Error installing dependencies: {str(e)}"
                bot.send_message(chat_id, error_msg)
                logger.error(f"Error installing dependencies: {str(e)}")
                return False
        return True

    @error_handler
    def run_project(self, project_dir: str, chat_id: int, duration_days: int = None, auto_restart: bool = True):
        """Run project with specified duration"""
        main_files = []
        user_id = self.get_user_id_by_chat_id(chat_id)
        
        # Find main files in user_projects
        if user_id in self.user_projects:
            for project_info in self.user_projects[user_id]:
                if project_info['project_dir'] == project_dir:
                    main_files = project_info['main_files']
                    break

        # Check waiting_for_main_file for main files
        if not main_files and user_id in self.waiting_for_main_file:
            if 'scripts_to_run' in self.waiting_for_main_file[user_id]:
                main_files = self.waiting_for_main_file[user_id]['scripts_to_run']
            
            if main_files and user_id not in self.user_projects:
                self.user_projects[user_id] = []
                
            if main_files and user_id in self.user_projects:
                self.user_projects[user_id].append({
                    'project_dir': project_dir,
                    'project_name': os.path.basename(project_dir),
                    'upload_time': datetime.now().isoformat(),
                    'chat_id': chat_id,
                    'pinned': False,
                    'main_files': main_files,
                    'num_scripts': len(main_files)
                })
                self.save_user_projects()

        if not main_files:
            bot.send_message(chat_id, "⚠️ No main files specified for the project")
            logger.error(f"No main files found for project: {project_dir}")
            return False

        # Verify main files exist
        missing_files = [f for f in main_files if not os.path.exists(f)]
        if missing_files:
            bot.send_message(chat_id, f"⚠️ Missing files: {', '.join(missing_files)}")
            logger.error(f"Missing main files: {missing_files}")
            return False

        if not self.install_requirements(project_dir, chat_id):
            return False

        try:
            logger.info(f"Running project: {main_files}")
            
            # Run all specified scripts
            processes = []
            for main_file in main_files:
                abs_main_file = os.path.abspath(main_file)
                if not os.path.isfile(abs_main_file):
                    bot.send_message(chat_id, f"⚠️ File not found: {abs_main_file}")
                    logger.error(f"File not found: {abs_main_file}")
                    continue
                    
                process = subprocess.Popen(
                    ['python', abs_main_file],
                    cwd=os.path.dirname(abs_main_file) or project_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )
                processes.append(process)
            
            # Optional Docker integration (uncomment if needed)
            """
            client = docker.from_env()
            container = client.containers.run(
                image="python:3.9-slim",
                command=["python", "/app/" + main_files[0]],
                volumes={os.path.abspath(project_dir): {'bind': '/app', 'mode': 'rw'}},
                detach=True
            )
            processes = [container]
            logger.info(f"Started Docker container {container.id} for project: {project_dir}")
            """

            if not processes:
                bot.send_message(chat_id, "❌ No files were executed, check file availability")
                return False
            
            end_time = datetime.now() + timedelta(days=duration_days) if duration_days else None
            
            self.running_processes[project_dir] = {
                'processes': processes,
                'chat_id': chat_id,
                'start_time': datetime.now(),
                'end_time': end_time,
                'project_name': os.path.basename(project_dir),
                'user_id': user_id,
                'pinned': False,
                'main_files': main_files,
                'auto_restart': auto_restart
            }
            
            if project_dir in self.paused_processes:
                del self.paused_processes[project_dir]
            
            for process in processes:
                self.start_output_reader(process, chat_id, os.path.basename(project_dir))
            
            logger.info(f"Project started successfully: {project_dir}")
            bot.send_message(chat_id, f"✅ Started project: {os.path.basename(project_dir)}")
            return True
            
        except Exception as e:
            error_msg = f"⚠️ Failed to start project: {str(e)}"
            bot.send_message(chat_id, error_msg)
            logger.error(f"Failed to start project: {str(e)}")
            return False

    @error_handler
    def start_output_reader(self, process, chat_id, project_name):
        """Read project output and send errors to user"""
        def reader():
            try:
                while True:
                    output = process.stdout.readline()
                    if output == '' and process.poll() is not None:
                        break
                    if output:
                        logger.info(f"Output from {project_name}: {output.strip()}")
                
                error = process.stderr.read()
                if error:
                    logger.error(f"Error from {project_name}: {error}")
                    bot.send_message(chat_id, f"❌ Error in project {project_name}:\n{error[:3000]}")
                    
                if process.returncode != 0:
                    bot.send_message(chat_id, f"⚠️ Project {project_name} stopped with exit code: {process.returncode}")
            except Exception as e:
                logger.error(f"Error in output reader: {str(e)}")

        thread = threading.Thread(target=reader)
        thread.daemon = True
        thread.start()

    @error_handler
    def stop_project(self, project_dir: str, chat_id: int, pause=False):
        """Stop a running project"""
        if project_dir in self.running_processes:
            process_info = self.running_processes[project_dir]
            
            try:
                logger.info(f"Stopping project: {project_dir}")
                
                for process in process_info['processes']:
                    try:
                        process.terminate()
                        process.wait(timeout=5)
                    except Exception as e:
                        logger.error(f"Error stopping process: {str(e)}")
                        try:
                            process.kill()
                        except:
                            pass
                
                # Optional Docker stop (uncomment if using Docker)
                """
                if hasattr(process_info['processes'][0], 'stop'):
                    process_info['processes'][0].stop()
                    process_info['processes'][0].remove()
                    logger.info(f"Stopped and removed Docker container for project: {project_dir}")
                """
                
                if pause:
                    self.paused_processes[project_dir] = process_info
                
                del self.running_processes[project_dir]
                
                if not pause:
                    bot.send_message(chat_id, f"⏹️ Project stopped: {os.path.basename(project_dir)}")
                    logger.info(f"Project stopped: {project_dir}")
                return True
            except Exception as e:
                error_msg = f"❌ Failed to stop project: {str(e)}"
                bot.send_message(chat_id, error_msg)
                logger.error(f"Failed to stop project: {str(e)}")
                return False
        else:
            bot.send_message(chat_id, "⚠️ No running project found with this name")
            return False

    def get_user_id_by_chat_id(self, chat_id: int) -> int:
        """Get user ID from chat ID"""
        for user_id, projects in self.user_projects.items():
            for project in projects:
                if project['chat_id'] == chat_id:
                    return user_id
        return None

    def cleanup(self):
        """Clean up resources on bot shutdown"""
        self.keep_running = False
        for project_dir in list(self.running_processes.keys()):
            self.stop_project(project_dir, self.running_processes[project_dir]['chat_id'])
        logger.info("Cleaned up all resources and stopped processes")

# ==========================
# Main Bot Class
# ==========================
class PythonHostingBot:
    """Main bot class with user interface"""
    def __init__(self):
        self.user_manager = UserManager()
        self.manager = ProjectManager(self.user_manager)
        self.setup_handlers()

    def check_access(self, user_id: int) -> bool:
        return self.user_manager.is_allowed(user_id)

    def setup_handlers(self):
        @bot.message_handler(commands=['start', 'help'])
        @error_handler
        def start(message):
            if not self.check_access(message.from_user.id):
                bot.reply_to(message, "⛔ Sorry, you don't have access to this bot.")
                return
                
            welcome_msg = """
            👑 <b>Welcome to the HOST S1X AMIN Project Hosting System</b> 👑
            
<b>Features:</b>
- Complete ownership system 👩‍💻
- Support for multi-file projects 📁
- Manual main file selection 👆
- Install dependencies from requirements.txt ✍⌨️
- Set execution duration 📶
- Enhanced interactive interface ⭐
- Periodic restart every 10 minutes 🚀
            
<b>Available Commands:</b>
/start - Show this message 📃  
/myprojects - List your projects 🏴‍☠️ 
/stopall - Stop all your projects ❌  
/pause - Pause projects temporarily ⛔
/on - Resume paused projects 🔁  
/clear - Delete all projects 🚫
            
<b>Admin Commands:</b>
/adduser [id] - Add a user  
/removeuser [id] - Remove a user  
/listusers - List all users
            
Send your project files as a ZIP to start.

<b>System Name:</b> HOST S1X AMIN
            """
            bot.reply_to(message, welcome_msg, parse_mode='HTML')
            logger.info(f"Displayed welcome message for user: {message.from_user.id}")

        @bot.message_handler(commands=['adduser'])
        @error_handler
        def add_user_command(message):
            """Add a new user"""
            if message.from_user.id not in ADMIN_IDS:
                bot.reply_to(message, "⛔ This command is for admins only")
                return
            
            try:
                user_id = int(message.text.split()[1])
                self.user_manager.add_user(user_id)
                bot.reply_to(message, f"✅ Added user: {user_id}")
                logger.info(f"Added new user: {user_id}")
            except (IndexError, ValueError):
                bot.reply_to(message, "❌ Usage: /adduser <user_id>")
            except Exception as e:
                bot.reply_to(message, f"❌ Error: {str(e)}")

        @bot.message_handler(commands=['removeuser'])
        @error_handler
        def remove_user_command(message):
            """Remove a user"""
            if message.from_user.id not in ADMIN_IDS:
                bot.reply_to(message, "⛔ This command is for admins only")
                return
            
            try:
                user_id = int(message.text.split()[1])
                if self.user_manager.remove_user(user_id):
                    bot.reply_to(message, f"✅ Removed user: {user_id}")
                    logger.info(f"Removed user: {user_id}")
                else:
                    bot.reply_to(message, "❌ Cannot remove primary admins")
            except (IndexError, ValueError):
                bot.reply_to(message, "❌ Usage: /removeuser <user_id>")
            except Exception as e:
                bot.reply_to(message, f"❌ Error: {str(e)}")

        @bot.message_handler(commands=['listusers'])
        @error_handler
        def list_users_command(message):
            """List all users"""
            if message.from_user.id not in ADMIN_IDS:
                bot.reply_to(message, "⛔ This command is for admins only")
                return
            
            users = self.user_manager.list_users()
            response = "👥 <b>Authorized Users:</b>\n\n"
            for user_id in users:
                status = "👑 Admin" if user_id in ADMIN_IDS else "👤 User"
                response += f"- {user_id} ({status})\n"
            
            bot.reply_to(message, response, parse_mode='HTML')

        @bot.message_handler(commands=['myprojects'])
        @error_handler
        def show_user_projects(message):
            if not self.check_access(message.from_user.id):
                bot.reply_to(message, "⛔ Sorry, you don't have access to this bot.")
                return
                
            user_id = message.from_user.id
            if user_id not in self.manager.user_projects or not self.manager.user_projects[user_id]:
                bot.reply_to(message, "📂 You have no stored projects.")
                return
                
            response = "📂 <b>Your Projects:</b>\n\n"
            
            for idx, project_info in enumerate(self.manager.user_projects[user_id], 1):
                project_dir = project_info['project_dir']
                project_name = os.path.basename(project_dir)
                is_running = project_dir in self.manager.running_processes
                is_paused = project_dir in self.manager.paused_processes
                
                if is_running:
                    status = "🟢 Running"
                    if self.manager.running_processes[project_dir]['end_time']:
                        remaining = self.manager.running_processes[project_dir]['end_time'] - datetime.now()
                        status += f" (Remaining: {remaining.days} days)"
                elif is_paused:
                    status = "🟡 Paused"
                else:
                    status = "🔴 Stopped"
                
                response += f"{idx}. <b>{project_name}</b> - {status}\n"
                
                keyboard = []
                if is_running:
                    keyboard.append([
                        InlineKeyboardButton("⏹️ Stop", callback_data=f'stop_{project_dir}'),
                        InlineKeyboardButton("⏸️ Pause", callback_data=f'pause_{project_dir}')
                    ])
                    keyboard.append([
                        InlineKeyboardButton("⏳ Set Duration", callback_data=f'duration_{project_dir}'),
                        InlineKeyboardButton("🔄 Restart", callback_data=f'restart_{project_dir}')
                    ])
                elif is_paused:
                    keyboard.append([
                        InlineKeyboardButton("▶️ Resume", callback_data=f'resume_{project_dir}'),
                        InlineKeyboardButton("⏹️ Stop", callback_data=f'stop_{project_dir}')
                    ])
                else:
                    keyboard.append([
                        InlineKeyboardButton("▶️ Run", callback_data=f'run_{project_dir}'),
                        InlineKeyboardButton("🗑️ Delete", callback_data=f'delete_{project_dir}')
                    ])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                bot.send_message(message.chat.id, response, parse_mode='HTML', reply_markup=reply_markup)
                response = ""
            
            if not response:
                return
                
            bot.send_message(message.chat.id, response, parse_mode='HTML')

        @bot.message_handler(content_types=['document'])
        @error_handler
        def handle_document(message):
            if not self.check_access(message.from_user.id):
                bot.reply_to(message, "⛔ Sorry, you don't have access to this bot.")
                return
                
            file_name = message.document.file_name
            if not file_name.endswith('.zip'):
                bot.reply_to(message, "⚠️ Please send a ZIP file only")
                return
                
            try:
                temp_dir = tempfile.mkdtemp()
                temp_zip_path = os.path.join(temp_dir, file_name)
                
                file_info = bot.get_file(message.document.file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                
                with open(temp_zip_path, 'wb') as new_file:
                    new_file.write(downloaded_file)
                
                project_name = os.path.splitext(file_name)[0]
                user_dir = os.path.join(PROJECTS_DIR, str(message.from_user.id))
                os.makedirs(user_dir, exist_ok=True)
                project_dir = os.path.join(user_dir, project_name)
                
                if os.path.exists(project_dir):
                    shutil.rmtree(project_dir)
                
                with zipfile.ZipFile(temp_zip_path, 'r') as zip_ref:
                    zip_ref.extractall(project_dir)
                
                os.remove(temp_zip_path)
                os.rmdir(temp_dir)
                
                self.manager.waiting_for_main_file[message.from_user.id] = {
                    'project_dir': project_dir,
                    'project_name': project_name,
                    'chat_id': message.chat.id,
                    'scripts_to_run': []
                }
                
                keyboard = [
                    [InlineKeyboardButton("One Script", callback_data=f'scriptnum_1_{project_dir}')],
                    [InlineKeyboardButton("Two Scripts", callback_data=f'scriptnum_2_{project_dir}')]
                ]
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                bot.send_message(
                    message.chat.id,
                    f"📦 Received project: {project_name}\n\n"
                    "🔢 Please select the number of scripts to run:",
                    reply_markup=reply_markup
                )
                logger.info(f"Received new project from user: {message.from_user.id}")
                
            except Exception as e:
                error_msg = f"❌ Failed to process file: {str(e)}"
                bot.reply_to(message, error_msg)
                logger.error(f"Failed to process file: {str(e)}")
                if 'temp_dir' in locals() and os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)

        @bot.callback_query_handler(func=lambda call: call.data.startswith('scriptnum_'))
        @error_handler
        def handle_script_number(call):
            user_id = call.from_user.id
            if user_id not in self.manager.waiting_for_main_file:
                bot.answer_callback_query(call.id, "❌ Session expired, please resend the project")
                return
            
            parts = call.data.split('_', 2)
            num_scripts = int(parts[1])
            project_dir = parts[2]
            
            self.manager.waiting_for_main_file[user_id]['num_scripts'] = num_scripts
            
            python_scripts = self.manager.get_python_scripts(project_dir)
            
            if not python_scripts:
                bot.edit_message_text(
                    "⚠️ No Python (.py) files found in the project",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id
                )
                return
            
            if num_scripts == 1:
                keyboard = []
                for script in python_scripts:
                    data_str = f"{project_dir}|{script}"
                    data_hash = hashlib.md5(data_str.encode()).hexdigest()
                    self.manager.script_hashes[data_hash] = (project_dir, script)
                    keyboard.append([InlineKeyboardButton(script, callback_data=f'scriptselect_{data_hash}')])
                
                keyboard.append([InlineKeyboardButton("Cancel", callback_data='cancel_selection')])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                bot.edit_message_text(
                    "📂 Please select the main script:",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=reply_markup
                )
            else:
                self.manager.waiting_for_main_file[user_id]['available_scripts'] = python_scripts
                bot.edit_message_text(
                    "📂 Please send the names of the two scripts to run (e.g., main.py worker.py)\n"
                    "Available scripts:\n" + "\n".join(python_scripts) + "\n\n"
                    "Or press /cancel to cancel",
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id
                )

        @bot.callback_query_handler(func=lambda call: call.data.startswith('scriptselect_'))
        @error_handler
        def handle_script_selection(call):
            user_id = call.from_user.id
            if user_id not in self.manager.waiting_for_main_file:
                bot.answer_callback_query(call.id, "❌ Session expired, please resend the project")
                return
            
            data_hash = call.data.split('_', 1)[1]
            
            if data_hash not in self.manager.script_hashes:
                bot.answer_callback_query(call.id, "❌ Invalid callback data")
                return
                
            project_dir, script_name = self.manager.script_hashes[data_hash]
            del self.manager.script_hashes[data_hash]
            
            script_path = os.path.join(project_dir, script_name)
            self.manager.waiting_for_main_file[user_id]['scripts_to_run'] = [script_path]
            
            keyboard = [
                [InlineKeyboardButton("1 Day", callback_data=f'duration_1_{project_dir}')],
                [InlineKeyboardButton("3 Days", callback_data=f'duration_3_{project_dir}')],
                [InlineKeyboardButton("7 Days", callback_data=f'duration_7_{project_dir}')],
                [InlineKeyboardButton("No Duration", callback_data=f'duration_0_{project_dir}')]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            bot.edit_message_text(
                f"📄 Selected main script: {script_name}\n\n"
                "⏳ Please select the execution duration:",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=reply_markup
            )

        @bot.message_handler(func=lambda message: message.from_user.id in self.manager.waiting_for_main_file and 
                            'num_scripts' in self.manager.waiting_for_main_file[message.from_user.id] and
                            self.manager.waiting_for_main_file[message.from_user.id]['num_scripts'] == 2)
        @error_handler
        def handle_two_scripts_names(message):
            if message.text == '/cancel':
                user_data = self.manager.waiting_for_main_file.pop(message.from_user.id)
                project_dir = user_data['project_dir']
                try:
                    shutil.rmtree(project_dir)
                    bot.send_message(message.chat.id, "❌ Operation cancelled and project deleted.")
                    logger.info(f"Cancelled project upload: {project_dir}")
                except Exception as e:
                    bot.send_message(message.chat.id, f"❌ Failed to delete project: {str(e)}")
                    logger.error(f"Failed to delete project: {str(e)}")
                return
                
            user_data = self.manager.waiting_for_main_file[message.from_user.id]
            project_dir = user_data['project_dir']
            project_name = user_data['project_name']
            chat_id = user_data['chat_id']
            available_scripts = user_data.get('available_scripts', [])
            
            script_names = message.text.strip().split()
            
            if len(script_names) != 2:
                bot.send_message(
                    message.chat.id,
                    "⚠️ Please send exactly two script names.\n"
                    "Or press /cancel to cancel"
                )
                return
            
            missing_files = []
            main_files = []
            
            for script_name in script_names:
                script_path = os.path.join(project_dir, script_name)
                if not os.path.exists(script_path):
                    missing_files.append(script_name)
                else:
                    main_files.append(script_path)
            
            if missing_files:
                bot.send_message(
                    message.chat.id,
                    f"⚠️ Missing files: {', '.join(missing_files)}\n"
                    "Please send valid script names or /cancel to cancel"
                )
                return
            
            self.manager.waiting_for_main_file[message.from_user.id]['scripts_to_run'] = main_files
            
            keyboard = [
                [InlineKeyboardButton("1 Day", callback_data=f'duration_1_{project_dir}')],
                [InlineKeyboardButton("3 Days", callback_data=f'duration_3_{project_dir}')],
                [InlineKeyboardButton("7 Days", callback_data=f'duration_7_{project_dir}')],
                [InlineKeyboardButton("No Duration", callback_data=f'duration_0_{project_dir}')]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            bot.send_message(
                message.chat.id,
                f"📦 Project: {project_name}\n"
                f"📄 Main scripts: {', '.join(script_names)}\n\n"
                "⏳ Please select the execution duration:",
                reply_markup=reply_markup
            )

        @bot.callback_query_handler(func=lambda call: call.data.startswith('duration_') and not call.data.startswith('duration_set_'))
        @error_handler
        def handle_initial_duration(call):
            parts = call.data.split('_')
            days = int(parts[1])
            project_dir = '_'.join(parts[2:])
            chat_id = call.message.chat.id
            user_id = call.from_user.id
            
            if user_id not in self.manager.waiting_for_main_file:
                bot.answer_callback_query(call.id, "❌ Session expired, please resend the project")
                return
            
            user_data = self.manager.waiting_for_main_file[user_id]
            main_files = user_data.get('scripts_to_run', [])
            
            if not main_files:
                bot.answer_callback_query(call.id, "❌ No main script selected")
                return
            
            install_success = self.manager.install_requirements(project_dir, chat_id)
            
            if user_id not in self.manager.user_projects:
                self.manager.user_projects[user_id] = []
                
            self.manager.user_projects[user_id].append({
                'project_dir': project_dir,
                'project_name': os.path.basename(project_dir),
                'upload_time': datetime.now().isoformat(),
                'chat_id': chat_id,
                'pinned': False,
                'main_files': main_files,
                'num_scripts': len(main_files)
            })
            self.manager.save_user_projects()
            
            if user_id in self.manager.waiting_for_main_file:
                del self.manager.waiting_for_main_file[user_id]
            
            keyboard = [
                [InlineKeyboardButton("▶️ Run Now", callback_data=f'run_{project_dir}')],
                [InlineKeyboardButton("⏳ Run Later", callback_data=f'runlater_{project_dir}')]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            status_msg = "✅ Main scripts set and dependencies installed" if install_success else "⚠️ Main scripts set but some dependencies failed to install"
            duration_msg = f"⏳ Duration: {days} days" if days > 0 else "⏳ No duration set"
            
            bot.edit_message_text(
                f"{status_msg}\n{duration_msg}\n\n"
                f"📦 Project: {os.path.basename(project_dir)} ready to run\n"
                f"📄 Main scripts: {', '.join(os.path.basename(f) for f in main_files)}",
                chat_id=chat_id,
                message_id=call.message.message_id,
                reply_markup=reply_markup
            )

        @bot.callback_query_handler(func=lambda call: True)
        @error_handler
        def handle_callbacks(call):
            try:
                if call.data.startswith('run_'):
                    project_dir = call.data.split('_', 1)[1]
                    chat_id = call.message.chat.id
                    
                    bot.edit_message_text(
                        "⏳ Starting project...",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                    
                    success = self.manager.run_project(project_dir, chat_id)
                    
                    if not success:
                        bot.send_message(
                            chat_id,
                            "❌ Failed to start project, check logs"
                        )
                
                elif call.data.startswith('stop_'):
                    project_dir = call.data.split('_', 1)[1]
                    chat_id = call.message.chat.id
                    
                    bot.edit_message_text(
                        "⏳ Stopping project...",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                    
                    success = self.manager.stop_project(project_dir, chat_id)
                    
                    if success:
                        bot.edit_message_text(
                            f"⏹️ Project stopped: {os.path.basename(project_dir)}",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                        logger.info(f"Project stopped: {project_dir}")
                    else:
                        bot.edit_message_text(
                            f"❌ Failed to stop project: {os.path.basename(project_dir)}",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                
                elif call.data.startswith('pause_'):
                    project_dir = call.data.split('_', 1)[1]
                    chat_id = call.message.chat.id
                    
                    bot.edit_message_text(
                        "⏳ Pausing project...",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                    
                    success = self.manager.stop_project(project_dir, chat_id, pause=True)
                    
                    if success:
                        bot.edit_message_text(
                            f"⏸️ Project paused: {os.path.basename(project_dir)}",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                        logger.info(f"Project paused: {project_dir}")
                    else:
                        bot.edit_message_text(
                            f"❌ Failed to pause project: {os.path.basename(project_dir)}",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                
                elif call.data.startswith('resume_'):
                    project_dir = call.data.split('_', 1)[1]
                    chat_id = call.message.chat.id
                    
                    bot.edit_message_text(
                        "⏳ Resuming project...",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                    
                    if project_dir in self.manager.paused_processes:
                        process_info = self.manager.paused_processes[project_dir]
                        success = self.manager.run_project(
                            project_dir,
                            process_info['chat_id'],
                            (process_info['end_time'] - datetime.now()).days if process_info.get('end_time') else None
                        )
                        
                        if success:
                            bot.edit_message_text(
                                f"▶️ Project resumed: {os.path.basename(project_dir)}",
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id
                            )
                            logger.info(f"Project resumed: {project_dir}")
                        else:
                            bot.edit_message_text(
                                f"❌ Failed to resume project: {os.path.basename(project_dir)}",
                                chat_id=call.message.chat.id,
                                message_id=call.message.message_id
                            )
                    else:
                        bot.edit_message_text(
                            "⚠️ No paused project found with this name",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                
                elif call.data.startswith('restart_'):
                    project_dir = call.data.split('_', 1)[1]
                    chat_id = call.message.chat.id
                    
                    bot.edit_message_text(
                        "⏳ Restarting project...",
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                    
                    if project_dir in self.manager.running_processes:
                        self.manager.stop_project(project_dir, chat_id)
                    
                    success = self.manager.run_project(project_dir, chat_id)
                    
                    if success:
                        bot.edit_message_text(
                            f"🔄 Project restarted: {os.path.basename(project_dir)}",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                        logger.info(f"Project restarted: {project_dir}")
                    else:
                        bot.edit_message_text(
                            f"❌ Failed to restart project: {os.path.basename(project_dir)}",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                
                elif call.data.startswith('duration_set_'):
                    parts = call.data.split('_')
                    days = int(parts[2])
                    project_dir = '_'.join(parts[3:])
                    chat_id = call.message.chat.id
                    
                    if project_dir in self.manager.running_processes:
                        if days > 0:
                            self.manager.running_processes[project_dir]['end_time'] = datetime.now() + timedelta(days=days)
                            msg = f"⏳ Set duration to {days} days for project: {os.path.basename(project_dir)}"
                        else:
                            self.manager.running_processes[project_dir]['end_time'] = None
                            msg = f"⏳ Removed duration for project: {os.path.basename(project_dir)}"
                        
                        bot.edit_message_text(
                            msg,
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                        logger.info(msg)
                    else:
                        bot.edit_message_text(
                            f"⚠️ Cannot set duration for inactive project",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                
                elif call.data.startswith('delete_'):
                    project_dir = call.data.split('_', 1)[1]
                    chat_id = call.message.chat.id
                    user_id = self.manager.get_user_id_by_chat_id(chat_id)
                    
                    if user_id and user_id in self.manager.user_projects:
                        self.manager.user_projects[user_id] = [p for p in self.manager.user_projects[user_id] if p['project_dir'] != project_dir]
                        self.manager.save_user_projects()
                    
                    if project_dir in self.manager.running_processes:
                        self.manager.stop_project(project_dir, chat_id)
                    
                    if project_dir in self.manager.paused_processes:
                        del self.manager.paused_processes[project_dir]
                    
                    try:
                        shutil.rmtree(project_dir)
                        bot.edit_message_text(
                            f"🗑️ Project deleted: {os.path.basename(project_dir)}",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                        logger.info(f"Project deleted: {project_dir}")
                    except Exception as e:
                        bot.edit_message_text(
                            f"❌ Failed to delete project: {str(e)}",
                            chat_id=call.message.chat.id,
                            message_id=call.message.message_id
                        )
                        logger.error(f"Failed to delete project: {str(e)}")
                
                elif call.data == 'cancel_selection':
                    bot.delete_message(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id
                    )
                    
            except Exception as e:
                logger.error(f"Error handling callback: {str(e)}\n{traceback.format_exc()}")
                bot.answer_callback_query(call.id, "❌ Error processing your request")

    @error_handler
    def run(self):
        logger.info("Starting bot...")
        while True:
            try:
                bot.polling(none_stop=True, timeout=60)
            except Exception as e:
                logger.error(f"Error running bot: {str(e)}\n{traceback.format_exc()}")
                logger.info("Retrying in 10 seconds...")
                time.sleep(10)

if __name__ == '__main__':
    try:
        logger.info("Starting bot...")
        bot_instance = PythonHostingBot()
        
        try:
            bot_instance.run()
        except KeyboardInterrupt:
            logger.info("Received shutdown signal...")
            bot_instance.manager.cleanup()
            sys.exit(0)
            
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}\n{traceback.format_exc()}")
        sys.exit(1)