import pandas as pd


class PremiumEngine:
    def __init__(self, under_500k_path='risk1_base_rates.csv', over_500k_path='risk1_base_rates_over_500k.csv'):
        # Load both CSVs and set the 'Age' column as the index for quick lookups
        self.rates_under_500k = pd.read_csv(under_500k_path)
        self.rates_under_500k.set_index('Age', inplace=True)

        self.rates_over_500k = pd.read_csv(over_500k_path)
        self.rates_over_500k.set_index('Age', inplace=True)

    def get_base_premium(self, age, profile, coverage_amount):
        """
        Fetches the base Risk 1 premium for the given age and profile,
        automatically selecting the correct rate table based on coverage amount.
        """
        try:
            # Select the correct dataframe based on coverage amount
            if coverage_amount > 500000:
                dataset = self.rates_over_500k
            else:
                dataset = self.rates_under_500k

            # Look up the rate per 100,000 NIS
            rate_per_100k = dataset.loc[age, profile]

            # Calculate the total monthly premium
            return rate_per_100k * (coverage_amount / 100000)
        except KeyError:
            return None

    def calculate_new_member_discounts(self, age, profile, coverage_amount):
        """
        Applies the Year 1 (15%), Year 2 (10%), and Year 3 (5%) discounts
        used by both the Risk 10 and Risk 15 policies.
        """
        base_premium = self.get_base_premium(age, profile, coverage_amount)

        if base_premium is None:
            return "Age or profile not found in base rates."

        # Define the discount structure
        discounts = {
            "Year 1": 0.15,
            "Year 2": 0.10,
            "Year 3": 0.05
        }

        # Calculate the final premium for each year
        discounted_premiums = {}
        for year, discount in discounts.items():
            discounted_premiums[year] = round(base_premium * (1 - discount), 2)

        return discounted_premiums


# --- Example Usage ---
if __name__ == "__main__":
    # Initialize the engine (it will automatically look for the two CSV files)
    engine = PremiumEngine()

    age = 30
    profile = 'Male_NonSmoker'

    # Test Scenario 1: Coverage under 500,000
    coverage_low = 400000
    base_low = engine.get_base_premium(age, profile, coverage_low)
    print(f"Coverage: ₪{coverage_low:,} -> Base Premium: ₪{base_low:.2f}")

    # Test Scenario 2: Coverage over 500,000
    coverage_high = 1000000
    base_high = engine.get_base_premium(age, profile, coverage_high)
    print(f"Coverage: ₪{coverage_high:,} -> Base Premium: ₪{base_high:.2f}")