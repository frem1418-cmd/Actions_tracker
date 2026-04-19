@echo off
title Lancement Analyseur Bourse
echo Recherche de l'application dans %cd%...

:: On force l'utilisation de python pour lancer streamlit
python -m streamlit run app.py

:: Le "pause" permet de garder la fenetre ouverte si ca plante
echo.
echo Si l'application ne s'est pas ouverte, lis l'erreur ci-dessus.
::pause