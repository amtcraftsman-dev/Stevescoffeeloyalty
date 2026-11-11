# Deploy the Steve’s Coffee pilot to Render

## 1. Upload to GitHub

1. Unzip the download.
2. In GitHub, create a private repository such as `steves-coffee-app`. Open **Add file → Upload files**.
3. Upload the files **inside** the unzipped folder, including the whole `static` folder and the hidden `.gitignore` file. Commit the upload. Check that `render.yaml` and `server.py` appear at the top level of the repository.

## 2. Deploy from Render

1. In [Render Dashboard](https://dashboard.render.com/), choose **New → Blueprint**. Connect GitHub and select the new repository.
2. Review and deploy the Blueprint. It creates one paid Python web service with a 1 GB persistent disk. The disk keeps the SQLite data across service restarts and deployments. Review the total shown by Render before deploying.
3. Open the web service in Render. Under **Environment**, find the two generated values. Share `PILOT_CODE` with staff testers. Keep `STAFF_PIN` within the group that handles orders. Use the service's `onrender.com` URL as the customer link and append `/staff` for the staff screen.
4. Use the sample pickup hours for testing, or temporarily set `ALWAYS_OPEN=1` in the Render environment to test at any time. To check the deployed service, open `/health` and look for `{"status": "ok"}`.

## 3. Try a test order

1. Open the customer URL, enter the pilot code, and use **Rewards** to sign up with a sample name and a reserved fictional number, such as `202-555-0147`. Confirm the welcome points.
2. Add a drink, choose a pickup slot, and place the order. Open `/staff`, enter the pilot code if prompted, then enter the staff code.
3. Move the order through **Start → Ready → Picked up**. Check the customer's tracking screen and rewards history. A pickup earns points once. To try redemption, use test orders to reach a sample reward threshold. Cancel a reward order to see the points return.
4. On iPhone, open the customer URL in Safari and use **Share → Add to Home Screen**. On Android, open it in Chrome and use **Install app** or **Add to Home screen**, depending on the device. The installed web app uses the same Render backend. Ordering and rewards require a connection.

## Before customer launch

- Confirm whether Loyalzoo should continue holding the balances used at the Clover register. Ask Loyalzoo whether the shop can access a supported integration to read balances, award points, and redeem rewards from a custom app. If switching systems, plan how to carry over customer balances and how in-store purchases will earn points.
- Replace the sample menu, prices, hours, and reward rules. The app assumes pay at pickup and displays pre-tax totals.
- Add verified customer sign-in and account recovery, publish the applicable privacy information, and set up backups with a tested restore process before collecting real customer data.
- If order tickets or payments should go through Clover, validate those flows in Clover’s sandbox and test a real register before enabling them. Use the staff screen and sample data for the pilot.

References: [Render Blueprints](https://render.com/docs/infrastructure-as-code), [Render persistent disks](https://render.com/docs/disks), [Loyalzoo for Clover](https://loyalzoo.com/integrations/clover/).
