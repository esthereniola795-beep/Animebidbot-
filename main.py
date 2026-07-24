import os
import sys
import logging
import datetime
import asyncio
from typing import Dict, List, Optional, Tuple
from decimal import Decimal
import re
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)
from telegram.constants import ParseMode
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship, scoped_session
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import random

# Load environment variables
load_dotenv()

# Configure logging for Railway
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Database setup with Railway PostgreSQL
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    logger.warning("DATABASE_URL not found, using SQLite for development")
    DATABASE_URL = 'sqlite:///animebidbot.db'

# Handle Railway's PostgreSQL URL format
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Create engine with connection pooling for Railway
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    echo=False
)

Base = declarative_base()
SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

# Database Models
class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(100))
    full_name = Column(String(200))
    balance = Column(Float, default=1000.0)
    joined_date = Column(DateTime, default=datetime.datetime.utcnow)
    is_admin = Column(Boolean, default=False)
    total_bids = Column(Integer, default=0)
    items_won = Column(Integer, default=0)
    
    bids = relationship("Bid", back_populates="user", cascade="all, delete-orphan")
    items_listed = relationship("AnimeItem", foreign_keys="AnimeItem.seller_id", back_populates="seller")
    items_won_rel = relationship("AnimeItem", foreign_keys="AnimeItem.winner_id", back_populates="winner")

class AnimeItem(Base):
    __tablename__ = 'anime_items'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, default="No description provided")
    category = Column(String(100), nullable=False)
    image_url = Column(String(500), nullable=True)
    starting_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=False)
    seller_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    seller_username = Column(String(100))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    end_time = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True)
    is_ended = Column(Boolean, default=False)
    winner_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    total_bids = Column(Integer, default=0)
    
    seller = relationship("User", foreign_keys=[seller_id], back_populates="items_listed")
    winner = relationship("User", foreign_keys=[winner_id], back_populates="items_won_rel")
    bids = relationship("Bid", back_populates="item", cascade="all, delete-orphan")

class Bid(Base):
    __tablename__ = 'bids'
    
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey('anime_items.id'), nullable=False)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    amount = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    is_winning = Column(Boolean, default=False)
    
    user = relationship("User", back_populates="bids")
    item = relationship("AnimeItem", back_populates="bids")

# Create tables
Base.metadata.create_all(bind=engine)

# Bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID', 0))
RAILWAY_ENV = os.getenv('RAILWAY_ENVIRONMENT', 'development')

if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set in environment variables!")
    sys.exit(1)

# Constants
CATEGORIES = ['Figures', 'Posters', 'Cards', 'Plushies', 'Art', 'Manga', 'DVD/Blu-ray', 'Other']
BID_INCREMENT = 1.0
AUCTION_DURATION_HOURS = 48  # 2 days
MINIMUM_BID = 1.0

# Emojis
EMOJI = {
    'ANIME': '🎌',
    'BID': '💰',
    'CLOCK': '⏰',
    'TROPHY': '🏆',
    'WARNING': '⚠️',
    'SUCCESS': '✅',
    'ERROR': '❌',
    'GIFT': '🎁',
    'MONEY': '💵',
    'STAR': '⭐',
    'FIRE': '🔥',
    'CROWN': '👑',
    'FIGURE': '🗿',
    'POSTER': '🖼️',
    'CARDS': '🃏',
    'PLUSH': '🧸',
    'ART': '🎨',
    'MANGA': '📚',
    'DVD': '💿',
    'OTHER': '📦',
}

# Conversation states for adding items
SELECT_CATEGORY, ENTER_NAME, ENTER_DESCRIPTION, ENTER_PRICE, CONFIRM_ADD = range(5)

# Helper Functions
def get_category_emoji(category: str) -> str:
    """Get emoji for category."""
    emoji_map = {
        'Figures': EMOJI['FIGURE'],
        'Posters': EMOJI['POSTER'],
        'Cards': EMOJI['CARDS'],
        'Plushies': EMOJI['PLUSH'],
        'Art': EMOJI['ART'],
        'Manga': EMOJI['MANGA'],
        'DVD/Blu-ray': EMOJI['DVD'],
        'Other': EMOJI['OTHER'],
    }
    return emoji_map.get(category, EMOJI['OTHER'])

def format_time_left(end_time: datetime.datetime) -> str:
    """Format time left in a human-readable way."""
    now = datetime.datetime.utcnow()
    if end_time <= now:
        return "Ended"
    
    time_left = end_time - now
    days = time_left.days
    hours = time_left.seconds // 3600
    minutes = (time_left.seconds % 3600) // 60
    
    if days > 0:
        return f"{days}d {hours}h left"
    elif hours > 0:
        return f"{hours}h {minutes}m left"
    else:
        return f"{minutes}m left"

def get_user(session, telegram_id: int, username: str = None, full_name: str = None) -> User:
    """Get or create user."""
    user = session.query(User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            balance=1000.0
        )
        session.add(user)
        session.commit()
        logger.info(f"New user created: {username} (ID: {telegram_id})")
    elif username and (user.username != username):
        user.username = username
        session.commit()
    return user

# Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start command handler."""
    user = update.effective_user
    session = SessionLocal()
    
    try:
        db_user = get_user(session, user.id, user.username, user.full_name)
        
        welcome_text = f"""
{EMOJI['ANIME']} *Welcome to AnimeBidBot!* {EMOJI['ANIME']}

Your ultimate destination for bidding on rare anime merchandise!

📌 *Key Features:*
• 🗿 Bid on anime figures, posters, and more
• 💰 Virtual currency system
• ⏰ Automatic auction ending
• 🏆 Win exclusive items

💡 *Quick Start:*
Use the buttons below to get started!

💰 *Your Balance:* ${db_user.balance:.2f}
"""
        
        keyboard = [
            [
                InlineKeyboardButton("📋 Active Auctions", callback_data="view_auctions"),
                InlineKeyboardButton("💰 My Balance", callback_data="check_balance")
            ],
            [
                InlineKeyboardButton("➕ List Item", callback_data="start_add_item"),
                InlineKeyboardButton("📊 My Bids", callback_data="my_bids")
            ],
            [
                InlineKeyboardButton("📖 Help", callback_data="help_menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text(f"{EMOJI['ERROR']} An error occurred. Please try again.")
    finally:
        session.close()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Help command handler."""
    help_text = f"""
{EMOJI['ANIME']} *AnimeBidBot Help* {EMOJI['ANIME']}

*📋 Basic Commands:*
/start - Register and start the bot
/help - Show this help message
/list - View all active auctions
/balance - Check your balance
/bids - View your bids
/mylist - View your listed items

*🏷️ Adding Items:*
Use /add or the "Add Item" button to list new items

*💵 Bidding:*
Use /bid [item_number] [amount] or click buttons

*📂 Categories Available:*
{"".join(f"• {get_category_emoji(cat)} {cat}\n" for cat in CATEGORIES)}

*💰 Currency System:*
• Starting balance: 1000 coins
• Minimum bid: ${MINIMUM_BID:.2f}
• Bid increment: ${BID_INCREMENT:.2f}
• Auction duration: {AUCTION_DURATION_HOURS} hours
• Winner gets item + 10% cashback!

{EMOJI['FIRE']} Happy Bidding!
"""
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def list_auctions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active auctions."""
    session = SessionLocal()
    
    try:
        items = session.query(AnimeItem).filter(
            AnimeItem.is_active == True,
            AnimeItem.is_ended == False
        ).order_by(AnimeItem.created_at.desc()).all()
        
        if not items:
            await update.message.reply_text(
                f"{EMOJI['WARNING']} No active auctions at the moment!\n\n"
                f"Be the first to list an item using /add"
            )
            return
        
        message = f"{EMOJI['ANIME']} *Active Auctions* ({len(items)} items)\n\n"
        
        for idx, item in enumerate(items[:15], 1):
            emoji = get_category_emoji(item.category)
            time_left = format_time_left(item.end_time)
            
            message += f"*{idx}. {emoji} {item.name}*\n"
            message += f"   💰 ${item.current_price:.2f}\n"
            message += f"   ⏰ {time_left}\n"
            message += f"   📂 {item.category} | Bids: {item.total_bids}\n\n"
        
        if len(items) > 15:
            message += f"\n📌 *Showing 15 of {len(items)} items*"
        
        keyboard = [
            [InlineKeyboardButton("🔍 View Item Details", callback_data="view_auctions")],
            [InlineKeyboardButton("➕ List Your Item", callback_data="start_add_item")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error in list_auctions: {e}")
        await update.message.reply_text(f"{EMOJI['ERROR']} An error occurred. Please try again.")
    finally:
        session.close()

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check user balance."""
    user = update.effective_user
    session = SessionLocal()
    
    try:
        db_user = get_user(session, user.id, user.username, user.full_name)
        
        # Get statistics
        active_bids = session.query(Bid).filter_by(user_id=db_user.id).join(AnimeItem).filter(
            AnimeItem.is_active == True,
            AnimeItem.is_ended == False
        ).count()
        
        won_items = session.query(AnimeItem).filter_by(winner_id=db_user.id).count()
        total_spent = session.query(Bid).filter_by(user_id=db_user.id).with_entities(
            func.sum(Bid.amount)
        ).scalar() or 0
        
        message = f"""
{EMOJI['MONEY']} *Your Profile*

💰 *Balance:* ${db_user.balance:.2f}
📊 *Active Bids:* {active_bids}
🏆 *Items Won:* {won_items}
💸 *Total Spent:* ${total_spent:.2f}
📅 *Member Since:* {db_user.joined_date.strftime('%Y-%m-%d')}

⭐ *Tips:* 
• List items to earn coins
• Win auctions to get exclusive items
• Higher bids increase your chances!
"""
        
        keyboard = [
            [InlineKeyboardButton("📋 View Auctions", callback_data="view_auctions")],
            [InlineKeyboardButton("➕ List New Item", callback_data="start_add_item")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error in balance: {e}")
        await update.message.reply_text(f"{EMOJI['ERROR']} An error occurred.")
    finally:
        session.close()

async def my_bids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's active bids."""
    user = update.effective_user
    session = SessionLocal()
    
    try:
        db_user = get_user(session, user.id, user.username, user.full_name)
        
        bids = session.query(Bid).filter_by(user_id=db_user.id).join(AnimeItem).filter(
            AnimeItem.is_active == True,
            AnimeItem.is_ended == False
        ).order_by(Bid.timestamp.desc()).all()
        
        if not bids:
            await update.message.reply_text(
                f"{EMOJI['WARNING']} You have no active bids!\n\n"
                f"Check out /list to find items to bid on!"
            )
            return
        
        message = f"{EMOJI['BID']} *Your Active Bids* ({len(bids)} bids)\n\n"
        for idx, bid in enumerate(bids[:10], 1):
            emoji = get_category_emoji(bid.item.category)
            time_left = format_time_left(bid.item.end_time)
            
            message += f"*{idx}. {emoji} {bid.item.name}*\n"
            message += f"   Your Bid: ${bid.amount:.2f}\n"
            message += f"   Current: ${bid.item.current_price:.2f}\n"
            message += f"   ⏰ {time_left}\n\n"
        
        if len(bids) > 10:
            message += f"\n📌 *Showing 10 of {len(bids)} bids*"
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in my_bids: {e}")
        await update.message.reply_text(f"{EMOJI['ERROR']} An error occurred.")
    finally:
        session.close()

async def my_listings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show user's listed items."""
    user = update.effective_user
    session = SessionLocal()
    
    try:
        db_user = get_user(session, user.id, user.username, user.full_name)
        
        items = session.query(AnimeItem).filter_by(
            seller_id=db_user.id,
            is_ended=False
        ).order_by(AnimeItem.created_at.desc()).all()
        
        if not items:
            await update.message.reply_text(
                f"{EMOJI['WARNING']} You haven't listed any items!\n\n"
                f"Use /add to list your first item!"
            )
            return
        
        message = f"{EMOJI['GIFT']} *Your Listed Items* ({len(items)} items)\n\n"
        for idx, item in enumerate(items[:10], 1):
            emoji = get_category_emoji(item.category)
            bids_count = session.query(Bid).filter_by(item_id=item.id).count()
            time_left = format_time_left(item.end_time)
            
            message += f"*{idx}. {emoji} {item.name}*\n"
            message += f"   💰 ${item.current_price:.2f}\n"
            message += f"   📊 Bids: {bids_count}\n"
            message += f"   ⏰ {time_left}\n"
            message += f"   📂 {item.category}\n\n"
        
        keyboard = [[InlineKeyboardButton("➕ Add New Item", callback_data="start_add_item")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error in my_listings: {e}")
        await update.message.reply_text(f"{EMOJI['ERROR']} An error occurred.")
    finally:
        session.close()

async def bid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Place a bid on an item."""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            f"{EMOJI['ERROR']} *Usage:* /bid [item_number] [amount]\n\n"
            f"Example: /bid 1 75.50\n\n"
            f"Use /list to see available items and their numbers.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    try:
        item_idx = int(context.args[0]) - 1
        bid_amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI['ERROR']} Invalid format!\n"
            f"Use: /bid [item_number] [amount]\n"
            f"Example: /bid 1 75.50"
        )
        return
    
    if bid_amount < MINIMUM_BID:
        await update.message.reply_text(
            f"{EMOJI['ERROR']} Minimum bid is ${MINIMUM_BID:.2f}!"
        )
        return
    
    user = update.effective_user
    session = SessionLocal()
    
    try:
        db_user = get_user(session, user.id, user.username, user.full_name)
        
        # Get active items
        items = session.query(AnimeItem).filter(
            AnimeItem.is_active == True,
            AnimeItem.is_ended == False
        ).order_by(AnimeItem.created_at.desc()).all()
        
        if item_idx >= len(items) or item_idx < 0:
            await update.message.reply_text(
                f"{EMOJI['ERROR']} Invalid item number! Use /list to see available items."
            )
            return
        
        item = items[item_idx]
        
        # Check if user is the seller
        if item.seller_id == db_user.id:
            await update.message.reply_text(
                f"{EMOJI['WARNING']} You cannot bid on your own item!"
            )
            return
        
        # Validate bid
        if bid_amount <= item.current_price:
            await update.message.reply_text(
                f"{EMOJI['WARNING']} Bid must be higher than current price (${item.current_price:.2f})!\n"
                f"Minimum bid: ${item.current_price + BID_INCREMENT:.2f}"
            )
            return
        
        if bid_amount > db_user.balance:
            await update.message.reply_text(
                f"{EMOJI['ERROR']} Insufficient balance!\n"
                f"Your balance: ${db_user.balance:.2f}\n"
                f"Bid amount: ${bid_amount:.2f}"
            )
            return
        
        # Place bid
        new_bid = Bid(
            item_id=item.id,
            user_id=db_user.id,
            amount=bid_amount
        )
        session.add(new_bid)
        
        # Update item price and bid count
        item.current_price = bid_amount
        item.total_bids += 1
        
        # Update user balance
        db_user.balance -= bid_amount
        
        session.commit()
        
        # Send success message
        keyboard = [
            [InlineKeyboardButton("🔍 View Item", callback_data=f"view_item_{item.id}")],
            [InlineKeyboardButton("📋 All Auctions", callback_data="view_auctions")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"{EMOJI['SUCCESS']} *Bid Placed Successfully!*\n\n"
            f"📦 *Item:* {item.name}\n"
            f"{EMOJI['BID']} *Your Bid:* ${bid_amount:.2f}\n"
            f"💰 *New Price:* ${item.current_price:.2f}\n"
            f"{EMOJI['MONEY']} *Remaining Balance:* ${db_user.balance:.2f}\n"
            f"⏰ *Auction Ends:* {format_time_left(item.end_time)}\n\n"
            f"You're currently the highest bidder!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        
        # Notify previous highest bidder
        previous_highest = session.query(Bid).filter(
            Bid.item_id == item.id,
            Bid.user_id != db_user.id
        ).order_by(Bid.amount.desc()).first()
        
        if previous_highest:
            try:
                # Get previous bidder's user
                prev_user = session.query(User).filter_by(id=previous_highest.user_id).first()
                if prev_user:
                    # Return previous bidder's money (except bid amount - they can bid again)
                    prev_user.balance += previous_highest.amount
                    session.commit()
                    
                    # Notify via message (we can't send direct messages to users without chat_id)
                    logger.info(f"Outbid user {prev_user.telegram_id} on item {item.name}")
            except Exception as e:
                logger.error(f"Error returning previous bid: {e}")
        
    except Exception as e:
        logger.error(f"Error in bid command: {e}")
        await update.message.reply_text(f"{EMOJI['ERROR']} An error occurred. Please try again.")
        session.rollback()
    finally:
        session.close()

async def add_item_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the add item conversation."""
    # Show category selection
    keyboard = []
    for i, category in enumerate(CATEGORIES):
        emoji = get_category_emoji(category)
        keyboard.append([InlineKeyboardButton(f"{emoji} {category}", callback_data=f"cat_{category}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"{EMOJI['GIFT']} *Add New Item*\n\n"
        f"First, select a category for your item:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )
    
    return SELECT_CATEGORY

async def add_item_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle category selection."""
    query = update.callback_query
    await query.answer()
    
    category = query.data.replace("cat_", "")
    context.user_data['item_category'] = category
    
    emoji = get_category_emoji(category)
    
    await query.edit_message_text(
        f"{EMOJI['GIFT']} *Add New Item*\n\n"
        f"Category selected: {emoji} {category}\n\n"
        f"Now, enter the name of your item:",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return ENTER_NAME

async def add_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle item name entry."""
    context.user_data['item_name'] = update.message.text
    
    await update.message.reply_text(
        f"{EMOJI['GIFT']} *Add New Item*\n\n"
        f"Item name: {context.user_data['item_name']}\n\n"
        f"Now, enter a description for your item (optional, send /skip to skip):",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return ENTER_DESCRIPTION

async def add_item_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle item description entry."""
    if update.message.text.lower() == '/skip':
        context.user_data['item_description'] = "No description provided"
    else:
        context.user_data['item_description'] = update.message.text
    
    await update.message.reply_text(
        f"{EMOJI['GIFT']} *Add New Item*\n\n"
        f"Now, enter the starting price for your item (minimum ${MINIMUM_BID:.2f}):",
        parse_mode=ParseMode.MARKDOWN
    )
    
    return ENTER_PRICE

async def add_item_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle item price entry."""
    try:
        price = float(update.message.text)
        if price < MINIMUM_BID:
            await update.message.reply_text(
                f"{EMOJI['ERROR']} Price must be at least ${MINIMUM_BID:.2f}!\n"
                f"Please enter a valid price:"
            )
            return ENTER_PRICE
        
        context.user_data['item_price'] = price
        
        # Show confirmation
        item_name = context.user_data['item_name']
        category = context.user_data['item_category']
        description = context.user_data.get('item_description', 'No description')
        price = context.user_data['item_price']
        
        emoji = get_category_emoji(category)
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="confirm_add"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_add")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"{EMOJI['GIFT']} *Confirm Your Item*\n\n"
            f"📦 *Name:* {item_name}\n"
            f"📂 *Category:* {emoji} {category}\n"
            f"📝 *Description:* {description[:200]}{'...' if len(description) > 200 else ''}\n"
            f"💰 *Starting Price:* ${price:.2f}\n"
            f"⏰ *Duration:* {AUCTION_DURATION_HOURS} hours\n\n"
            f"Please confirm your listing:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        
        return CONFIRM_ADD
        
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI['ERROR']} Invalid price!\n"
            f"Please enter a valid number (e.g., 25.50):"
        )
        return ENTER_PRICE

async def add_item_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm and add the item."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "cancel_add":
        await query.edit_message_text(
            f"{EMOJI['ERROR']} Item listing cancelled."
        )
        return ConversationHandler.END
    
    user = update.effective_user
    session = SessionLocal()
    
    try:
        db_user = get_user(session, user.id, user.username, user.full_name)
        
        # Create the item
        item = AnimeItem(
            name=context.user_data['item_name'],
            description=context.user_data.get('item_description', 'No description'),
            category=context.user_data['item_category'],
            starting_price=context.user_data['item_price'],
            current_price=context.user_data['item_price'],
            seller_id=db_user.id,
            seller_username=user.username or user.full_name,
            end_time=datetime.datetime.utcnow() + datetime.timedelta(hours=AUCTION_DURATION_HOURS)
        )
        
        session.add(item)
        session.commit()
        
        emoji = get_category_emoji(item.category)
        
        # Send success message
        await query.edit_message_text(
            f"{EMOJI['SUCCESS']} *Item Listed Successfully!*\n\n"
            f"📦 *{item.name}*\n"
            f"{emoji} *Category:* {item.category}\n"
            f"💰 *Starting Price:* ${item.starting_price:.2f}\n"
            f"⏰ *Auction Ends:* {item.end_time.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Share your item with friends or wait for bidders!\n"
            f"Use /list to view all auctions.",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.error(f"Error adding item: {e}")
        await query.edit_message_text(
            f"{EMOJI['ERROR']} An error occurred while listing your item."
        )
        session.rollback()
    finally:
        session.close()
        context.user_data.clear()
    
    return ConversationHandler.END

async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the add item conversation."""
    await update.message.reply_text(
        f"{EMOJI['ERROR']} Item listing cancelled."
    )
    context.user_data.clear()
    return ConversationHandler.END

async def view_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View detailed item information."""
    query = update.callback_query
    await query.answer()
    
    try:
        # Extract item ID from callback data
        item_id = int(query.data.replace("view_item_", ""))
        
        session = SessionLocal()
        item = session.query(AnimeItem).filter_by(id=item_id).first()
        
        if not item:
            await query.edit_message_text(f"{EMOJI['ERROR']} Item not found!")
            session.close()
            return
        
        emoji = get_category_emoji(item.category)
        time_left = format_time_left(item.end_time)
        total_bids = session.query(Bid).filter_by(item_id=item.id).count()
        
        # Get highest bidder
        highest_bid = session.query(Bid).filter_by(item_id=item.id).order_by(Bid.amount.desc()).first()
        highest_bidder = None
        if highest_bid:
            bidder = session.query(User).filter_by(id=highest_bid.user_id).first()
            highest_bidder = bidder.username or bidder.full_name if bidder else "Unknown"
        
        message = f"""
{emoji} *{item.name}*

📂 *Category:* {item.category}
💰 *Current Price:* ${item.current_price:.2f}
📊 *Total Bids:* {total_bids}
👤 *Seller:* {item.seller_username}
⏰ *Time Left:* {time_left}

📝 *Description:*
{item.description}

{'🏆 *Highest Bidder:* ' + highest_bidder if highest_bidder else '💡 *No bids yet!*'}
"""
        
        keyboard = [
            [
                InlineKeyboardButton(f"💰 Bid Now", callback_data=f"bid_item_{item.id}"),
                InlineKeyboardButton("📋 All Auctions", callback_data="view_auctions")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
        session.close()
    except Exception as e:
        logger.error(f"Error viewing item: {e}")
        await query.edit_message_text(f"{EMOJI['ERROR']} An error occurred.")

async def bid_item_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bid button click."""
    query = update.callback_query
    await query.answer()
    
    item_id = int(query.data.replace("bid_item_", ""))
    
    await query.edit_message_text(
        f"{EMOJI['BID']} *Place Your Bid*\n\n"
        f"To bid on this item, use:\n"
        f"/bid [item_number] [amount]\n\n"
        f"First, find the item number using /list\n"
        f"Then place your bid with the amount you want to offer.",
        parse_mode=ParseMode.M
