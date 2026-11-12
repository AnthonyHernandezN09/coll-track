# Coll-Track

Coll-Track is an experimental macOS app for tracking college application portals in one place.

**Early prototype. Still under development.** This build is for local testing only and is far from a finished product. Portal coverage is incomplete. Sign-in can fail, and checks can miss changes or flag harmless page updates. Use the university portal to confirm deadlines, checklist requirements, and admissions decisions.

## What you can try

- Add universities to a dashboard, rearrange their cards, and customize their colors and logos. Card order is saved on your Mac.
- Import a checklist from an open Chrome tab and optionally export it as JSON.
- Save a login for one school or share a profile across schools. **Quick launch** can use a saved profile on supported Slate login pages.
- Schedule checks for individual portals. The first check creates a baseline. Later checks compare visible page text, link and image addresses, and checklist rows. Changes appear under **Unread**, and the Mac service saves the new snapshot.

## Set up locally

You’ll need macOS, Python 3.9 or later, Xcode command-line tools, and Chrome for the extension. From the repository root, run:

```sh
python3 backend/install-macos.py
```

The installer sets up Playwright and headless Chromium, compiles the credential encryption helper, and registers a macOS LaunchAgent. Open the [local dashboard](http://127.0.0.1:46387/).

In Chrome, open `chrome://extensions`, turn on **Developer mode**, then select **Load unpacked** and choose the repository folder. Use the extension’s **Manage logins** to save portal credentials.

## Track a portal

A fresh install starts with an empty school list.

1. Click **+** on the dashboard and add the school’s application status URL. You can also import a checklist from a signed-in tab using the extension.
2. Click **Enable checks** on the school card.
3. Open the top-right **Settings** menu to set check intervals, pause a school, or choose its saved login.

The Mac service runs scheduled checks while your Mac is awake and you’re signed in to macOS. Chrome and the dashboard can be closed. Portals that require verification or use a different sign-in flow may need manual attention.

## Local data

The dashboard listens on localhost and has no app sign-in. The service stores snapshots, settings, and encrypted login profiles under `~/Library/Application Support/Coll-Track`. Keep that directory and exported checklist files out of Git. Brandfetch is optional and requires your own API key.

## License and university portal use

Coll-Track is released under the [MIT License](LICENSE). You may use, modify, share, or sell the code, including in commercial projects. Keep the license and copyright notice crediting Anthony Hernandez with copies or substantial portions of the code.

Coll-Track and its browser extension interact with university portals, including signing in and reading application information. Use them only with accounts you are authorized to access and in accordance with each university’s rules. Universities control their portals and may restrict automated access or take action on accounts or applications.

To the extent permitted by applicable law, Anthony Hernandez is not liable for lost, corrupted, or unavailable information, or for any action taken by a university in connection with your use of Coll-Track, including blocked access, account suspension, or action affecting an application. Coll-Track is provided as is, without warranty, under the terms of the MIT License.
