from slackarxiv_simon import main_slack_one_time_send


def main():
    try:
        main_slack_one_time_send()
        print("Message sent to Slack successfully.")
    except Exception as e:
        print(f"Error in main_slack_one_time_send: {e}")

if __name__ == "__main__":
    main()