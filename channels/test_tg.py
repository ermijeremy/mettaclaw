import os
import time
from tg_channel import start_telegram, getLastMessage, stop_telegram

def main():
    token = ""
    if not token:
        print("Please set the TG_BOT_TOKEN environment variable.")
        return

    print("Starting Telegram bot...")
    start_telegram(token, "5116139198")

    print("Listening for batched messages. Press Ctrl+C to stop.")
    try:
        while True:
            msg = getLastMessage()
            if msg is not None:
                print(f"--- Received Batch ---\n{msg}\n----------------------")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping bot...")
    finally:
        stop_telegram()
        print("Bot stopped.")

if __name__ == "__main__":
    main()