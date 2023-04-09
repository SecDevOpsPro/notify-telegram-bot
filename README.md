# Telegram Bot for Personal Notifications

This is a simple Telegram bot that sends personal notifications to a user or a group. It can be used for various purposes such as reminders, alerts, and notifications.

## Features

- Send custom notifications to yourself or a Telegram group
- Set custom notification messages with optional variables
- Schedule notifications at specific times or intervals
- Get notified about important events or reminders

## Installation

1. Create a new bot on Telegram by talking to the BotFather and obtaining a bot token.
2. Clone this repository to your local machine:

git clone https://github.com/yourusername/telegram-bot-for-personal-notifications.git

3. Install the required dependencies:
4. Create a configuration file named `config.ini` in the project directory with the following contents:

```ini
[telegram]
api_key = YOUR_BOT_TOKEN
```

Replace `YOUR_BOT_TOKEN` with the bot token obtained in step 1.

## Usage

1. Update the `config.ini` file with your bot token.
2. Edit the `bot.py` file to customize your notification messages, schedule, and other settings.
3. Run the bot using the following command:

```sh
python bot.py
```

4. Start a chat with your bot on Telegram and send commands or messages to trigger notifications.

## Commands

The bot supports the following commands:

`/change`: Show current EUR change
<!--
`/help`: Show help and usage instructions
`/start`: Start the bot and get a welcome message
`/notify`: Send a custom notification with optional variables
`/schedule`: Schedule a notification at a specific time or interval
`/cancel`: Cancel a scheduled notification
-->

## License
This project is licensed under the MIT License.

## Contributing
Contributions are welcome! Please open an issue or submit a pull request for any improvements or bug fixes.

## Credits
This project uses the python-telegram-bot library for interacting with the Telegram Bot API.
