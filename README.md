# Background Removal API

A FastAPI-based application for removing backgrounds from images using the follwing a Swin Transformer-based architecture for high-quality background segmentation. This API provides a single endpoint to upload an image and receive a processed PNG with a transparent background.

## Features

- **High-Quality Background Removal**: Utilizes the BEN2 model for precise foreground extraction.
- **FastAPI Best Practices**: Implements dependency injection, error handling, and Swagger documentation.
- **Single Endpoint**: Simple POST endpoint for image processing.
- **Comprehensive Documentation**: Swagger UI available at `/docs` for easy API exploration.

## Prerequisites

- Python 3.12+
- CUDA-enabled GPU (optional, for faster processing; CPU fallback available)
- Dependencies listed in `requirements.txt`

## Installation

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/yourusername/background-removal-api.git
   cd background-removal-api

2. **Install Dependencies**:
    ```bash
    pip install -r requirements.txt

 
