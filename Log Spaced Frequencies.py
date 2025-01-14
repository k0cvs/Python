import numpy as np

# Ask the user for input
f_start = float(input("Enter the starting frequency (in Hz): "))
f_end = float(input("Enter the ending frequency (in Hz): "))
n = int(input("Enter the number of frequencies: "))

# Calculate the logarithmically spaced frequencies
frequencies = f_start * (f_end / f_start) ** (np.linspace(0, n-1, n) / (n-1))

# Print the frequencies as integers, one per line
for frequency in frequencies:
    print(int(frequency))
