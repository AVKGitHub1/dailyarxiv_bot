from slackarxiv_simon import main_ret_message, main_slack_one_time_send
import datetime

FILEPATH = "test_results.txt"

def get_test_message():
    try:
        message = main_ret_message()
        return message
    except Exception as e:
        print(f"Error in main_ret_message: {e}")
        return None

def save_msg(result):
    with open(FILEPATH, "a") as f:
        # clear the file before writing
        f.seek(0)
        f.truncate()
        # write the new message
        f.write(result)

def main():
    message = get_test_message()
    if message:
        print(f"Message retrieved successfully. Saved to {FILEPATH}.")
        save_msg("Retrieved Message:\n" + message)
    else:
        print("Failed to retrieve message.")

if __name__ == "__main__":
    main()