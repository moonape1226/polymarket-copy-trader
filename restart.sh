#!/bin/bash

# A simple script to restart the copy-trader container
# Use this after making changes to config.json or .env

echo "Restarting copy-trader container to apply configuration changes..."
docker compose restart copy-trader

echo ""
echo "Restart complete!"
echo "To view the latest logs, run: docker compose logs -f copy-trader"
