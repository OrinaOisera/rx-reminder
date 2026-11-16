# Demo script

Keep the recording under 3 minutes. Aim for about 2:50. Have two family members saved before you press record, and have the Bedrock model already enabled. Use a clear photo of a printed prescription. Do not show the API key on camera. Open the hosted page: https://d1l0axp05yrolu.cloudfront.net/

| Time | Length | What to show | What to say |
|---|---|---|---|
| 0:00 | 15s | A paper prescription next to the page. | A caregiver holds medicines for more than one person. A reminder that does not say who it is for is easy to mix up. |
| 0:15 | 15s | The architecture diagram in `docs/architecture.md`. | The page, the API, and the photo workflow are serverless. A photo is read by Textract and Bedrock, then the app stops and waits for a person. |
| 0:30 | 20s | Family list. Tap the person. Hold up the phone photo upload. | Each person has a name, an initial, and a colour. This prescription is for Amina. The name is typed here. It is not sent to the photo reader. |
| 0:50 | 15s | Step Functions console, state machine `rx-process-rx-reminder`, the run that just started. | The upload starts a workflow: read the text, structure the medicines, and stop at review. It does not create reminders. |
| 1:05 | 15s | Medicine cards. Point at a medium or low confidence badge and a field you correct. | The model leaves blanks instead of guessing. I check the dose against the label before I continue. |
| 1:20 | 25s | Schedule screen. Change one time. Tick "I have checked that this schedule is correct for Amina." Press confirm. | The timetable is generated from fixed rules. I confirm the schedule I was just shown, for Amina by name. |
| 1:45 | 30s | EventBridge Scheduler console, schedules named `rx-...`. Then two downloaded `.ics` files, or the page's calendar help, for two people. | Each dose is its own schedule. Each person gets a calendar file. Apple and Outlook use the colour in the file. Google ignores that colour, so each person gets a calendar named Meds and their name, and every title still starts with their emoji and name. |
| 2:15 | 20s | Press "Send me a test reminder now." Show the email or phone. | The reminder names the patient and the medicine. It is an aid. It tells you to follow the prescription. |
| 2:35 | 15s | Back to the page footer, then the architecture diagram. | When nobody is using it, there is no server running. Lambda, DynamoDB, and the API sit at zero until the next photo. |

Stop by 3:00. If you are running long, skip the second calendar and keep the test reminder.
