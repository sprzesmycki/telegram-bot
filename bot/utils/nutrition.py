from __future__ import annotations

def calculate_bmr(
    weight_kg: float, height_cm: float, age: int, gender: str
) -> float:
    """Mifflin-St Jeor Equation."""
    if gender.lower() == "male":
        return (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
    else:  # female
        return (10 * weight_kg) + (6.25 * height_cm) - (5 * age) - 161

def calculate_tdee(bmr: float, activity_level: str) -> float:
    multipliers = {
        "sedentary": 1.2,
        "light": 1.375,
        "moderate": 1.55,
        "active": 1.725,
        "very_active": 1.9,
    }
    return bmr * multipliers.get(activity_level.lower(), 1.2)

def calculate_macros(
    calories: float, weight_kg: float
) -> dict[str, float]:
    """
    Splits:
    - Protein: 2.0g per kg of body weight
    - Fat: 1.0g per kg of body weight
    - Carbs: remaining calories
    """
    protein_g = weight_kg * 2.0
    fat_g = weight_kg * 1.0
    
    protein_cal = protein_g * 4
    fat_cal = fat_g * 9
    
    carb_cal = max(0, calories - protein_cal - fat_cal)
    carb_g = carb_cal / 4
    
    return {
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carb_g,
    }
