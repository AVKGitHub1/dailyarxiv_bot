from slackbot_daily_arxiv import main_slack_send


def main():
    try:
        main_slack_send()
        print("Message sent to Slack successfully.")
    except Exception as e:
        print(f"Error in main_slack_send: {e}")

if __name__ == "__main__":
    main()