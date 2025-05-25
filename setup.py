from setuptools import setup, find_packages
import os

VERSION = '1.0.0'

setup(
    name="anime-downloader",
    version=VERSION,
    packages=find_packages(),
    install_requires=[
        'requests>=2.25.1',
        'beautifulsoup4>=4.9.3',
        'm3u8>=1.0.0',
        'ffmpeg-python>=0.2.0',
        'imageio-ffmpeg>=0.4.5',
        'tqdm>=4.62.3',
    ],
    entry_points={
        'console_scripts': [
            'anime-downloader=downloader:main',
        ],
    },
    author="Your Name",
    author_email="your.email@example.com",
    description="A cross-platform anime downloader",
    long_description=open('README.md').read(),
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/anime-downloader",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
)
