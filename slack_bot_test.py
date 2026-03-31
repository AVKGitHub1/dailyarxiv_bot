import sys

from slackbot_daily_arxiv import main_slack_send


def main():
    date_diff = int(sys.argv[1]) if len(sys.argv)>1 else None
    try:
        main_slack_send(date_diff=date_diff)
        print("Message sent to Slack successfully.")
    except Exception as e:
        print(f"Error in main_slack_send: {e}")

if __name__ == "__main__":
    main()