import sys

from slackbot_daily_arxiv import main_ret_message

FILEPATH = "test_results.txt"

def get_test_message(date_diff=None):
    try:
        message = main_ret_message(date_diff=date_diff)
        return message
    except Exception as e:
        print(f"Error in main_ret_message: {e}")
        return None

def save_msg(result):
    with open(FILEPATH, "a", encoding="utf-8") as f:
        # clear the file before writing
        f.seek(0)
        f.truncate()
        # write the new message
        f.write(result)

def main():
    date_diff = int(sys.argv[1]) if len(sys.argv)>1 else 0
    message = get_test_message(date_diff=date_diff)
    if message:
        print(f"Message retrieved successfully. Saved to {FILEPATH}.")
        save_msg("Retrieved Message:\n" + message)
    else:
        print("Failed to retrieve message.")

if __name__ == "__main__":
    main()