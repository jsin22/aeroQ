Act as an expert full-stack developer.

Please write the code for a full-stack web application that predicts airport security wait times based on flight departure volumes.

Tech Stack
Backend: Python with FastAPI.

Database/Cache: SQLite (mandatory for protecting free API limits).

Frontend: React (using Vite).

Deployment Target: Must be Docker-ready and compatible with PythonAnywhere.

The API Strategy (Free Tier Protection)
We are using the AirLabs API, which provides 1,000 free monthly requests.
To avoid burning through the rate limit, the backend MUST implement strict SQLite caching:

When a user inputs their airport and departure time, check the SQLite database for that specific airport's departure schedule.

If the data exists and the timestamp is less than 4 hours old, serve it entirely from the SQLite cache.

If the data is missing or stale, query the AirLabs /schedules endpoint for that airport, save the bulk schedule to SQLite with a new timestamp, and then serve the user's request from the fresh cache.

The Prediction Algorithm
Once you have the terminal's departure schedule from the database, calculate the crowd metrics:

Define the "Security Rush Window": Filter the schedule for all flights departing the user's specific terminal within a 2-hour window before their flight time.

Calculate "Estimated Passengers": Multiply the number of flights in that window by 150 (average aircraft seats). Multiply that result by 0.75 (assuming 25% of passengers are connecting and will not use the origin security checkpoint).

Calculate "Wait Category":

Assume the terminal has 5 security lanes open, processing roughly 150 passengers per lane per hour (750 total per hour capacity).

Compare the "Estimated Passengers" against the 750/hour capacity to return a label of "Light", "Moderate", or "Severe", alongside the raw passenger estimate.

Required Output
Please provide the following files in your response:

The requirements.txt for the Python backend.

The FastAPI backend code (main.py) including the complete SQLite caching logic and the prediction math.

A simple React frontend (App.jsx) with a clean form for the user to input their Airport Code (IATA), Terminal, and Flight Time, alongside a component to display the results.

A basic Dockerfile to containerize the application.

How to use this:
Sign up for a free API key at AirLabs.co.

Paste the prompt above into your AI assistant.

Take the generated Python code and React components, place them in your code editor, and add your API key to the environment variables.
