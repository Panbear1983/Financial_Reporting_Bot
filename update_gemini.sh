#!/bin/bash
# Script to update Gemini CLI

echo "Updating Gemini CLI to the latest version..."
npm install -g @google/gemini-cli@latest

echo "Verifying updated version..."
gemini --version

echo "Gemini CLI update complete."
