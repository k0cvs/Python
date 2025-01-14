def calculate_j_pole_dimensions(frequency):
    # Speed of light in meters per second
    c = 299792458
    
    # Convert frequency to Hz
    frequency_hz = frequency * 1e6
    
    # Calculate wavelength
    wavelength = c / frequency_hz
    
    # Calculate dimensions
    quarter_wavelength = wavelength / 4
    three_quarter_wavelength = 3 * quarter_wavelength
    
    # Calculate spacing between the two elements (typically 0.025 wavelength)
    spacing = 0.025 * wavelength
    
    return quarter_wavelength, three_quarter_wavelength, spacing

# Example usage
center_frequency = float(raw_input("Enter the center frequency in MHz: "))
quarter_wavelength, three_quarter_wavelength, spacing = calculate_j_pole_dimensions(center_frequency)

print "Quarter Wavelength: {} meters".format(quarter_wavelength)
print "Three Quarter Wavelength: {} meters".format(three_quarter_wavelength)
print "Required Spacing between the two elements: {} meters".format(spacing)